"""Identity keys, key delegation, and the signing roles.

The design keeps the roles separate rather than sharing one key: consumer keys
sign spend authorizations, contribution device keys sign usage reports, and the
platform settlement key signs receipts. A device key cannot redirect revenue,
and holding the settlement key does not let anyone raise a local contribution
limit.

An ``operator`` purpose used to exist here as a consumer-style request-signing
key, but no endpoint ever accepted it and neither the CLI nor the invite flow
could mint one, so it was documented-but-unusable. It has been removed rather
than left as a promise the platform cannot keep.

Public keys are only ever trusted through this registry. A receipt that carries
its own public key proves nothing: `verify_signed_record` looks the key up here
and fails closed when it is absent, revoked, or bound to a different identity.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from .canonical import base64_decode, base64_encode, verify_payload
from .errors import DelegationError, IdentityBindingError, KeyRevokedError, UnknownKeyError

KeyPurpose = Literal["consumer", "provider_device", "platform_settlement"]
KeyStatus = Literal["active", "revoked"]


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(dt.UTC)


@dataclass(frozen=True)
class IdentityKey:
    """A registered public key and the identity it is allowed to speak for."""

    key_id: str
    owner_id: str
    purpose: KeyPurpose
    public_key_b64: str
    registered_at: dt.datetime
    status: KeyStatus = "active"
    revoked_at: dt.datetime | None = None

    def public_key(self) -> Any:
        """Load the Ed25519 public key object.

        Raises:
            UnknownKeyError: If the stored bytes are not a valid Ed25519 key.
        """
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        raw = base64_decode(self.public_key_b64)
        if len(raw) != 32:
            raise UnknownKeyError(
                f"key {self.key_id}: Ed25519 public keys are 32 bytes, got {len(raw)}"
            )
        return Ed25519PublicKey.from_public_bytes(raw)

    def is_valid_at(self, moment: dt.datetime) -> bool:
        """Report whether the key was active at `moment`, honouring revocation.

        Revocation closes a window rather than erasing the key's whole history: a
        record signed strictly before `revoked_at` was signed by a key that was
        still good at the time, so it stays valid. A record signed at or after
        `revoked_at` does not. A key marked revoked but carrying no `revoked_at`
        has no window to test against, so it fails closed.

        Args:
            moment: The instant the record claims to have been signed.

        Returns:
            True when the key was registered by `moment` and not yet revoked at it.
        """
        if _as_utc(moment) < _as_utc(self.registered_at):
            return False
        if self.revoked_at is None:
            return self.status != "revoked"
        return _as_utc(moment) < _as_utc(self.revoked_at)


@dataclass(frozen=True)
class KeyDelegation:
    """A grant of signing authority from one identity to another key.

    A delegation can cap the milli-credit amount, restrict which models may be
    authorized, and expire. Scope checks are enforced at verification time, not
    only at issue time, so a leaked delegated key cannot authorize an unbounded
    spend after the fact.
    """

    delegation_id: str
    grantor_id: str
    delegatee_key_id: str
    purpose: KeyPurpose
    max_milli_credits: int | None
    allowed_models: frozenset[str] | None
    issued_at: dt.datetime
    expires_at: dt.datetime | None
    revoked_at: dt.datetime | None = None

    def is_valid_at(self, moment: dt.datetime) -> bool:
        if self.revoked_at is not None and _as_utc(moment) >= _as_utc(self.revoked_at):
            return False
        if _as_utc(moment) < _as_utc(self.issued_at):
            return False
        return not (
            self.expires_at is not None and _as_utc(moment) >= _as_utc(self.expires_at)
        )

    def check_scope(self, milli_credits: int, model: str | None) -> None:
        """Validate that a spend fits inside this delegation.

        Args:
            milli_credits: The amount the delegatee wants to authorize.
            model: The model being authorized, if any.

        Raises:
            DelegationError: If the amount or model exceeds the granted scope.
        """
        if self.max_milli_credits is not None and milli_credits > self.max_milli_credits:
            raise DelegationError(
                f"delegation {self.delegation_id} caps spend at {self.max_milli_credits} "
                f"milli-credits, requested {milli_credits}"
            )
        if (
            self.allowed_models is not None
            and model is not None
            and model not in self.allowed_models
        ):
            raise DelegationError(
                f"delegation {self.delegation_id} does not cover model {model!r}; "
                f"allowed: {sorted(self.allowed_models)}"
            )


@dataclass(frozen=True)
class SpendGrant:
    """Permission for one identity to charge an account it does not own.

    A valid signature proves *who signed*, not *whose money may be spent*. The
    design allows a team member to initiate a request while a team wallet pays,
    so consumer and payer may legitimately differ — but only where an explicit,
    revocable grant connects them. Without this record, any registered consumer
    key could name any funded account as its payer and drain it.

    An identity may always spend its own account; that case needs no grant. A
    grant exists solely for the cross-account case, and carries its own cap and
    lifetime so it can be withdrawn without touching the account itself.
    """

    payer_account_id: str
    spender_id: str
    granted_at: dt.datetime
    max_milli_credits: int | None = None
    expires_at: dt.datetime | None = None
    revoked_at: dt.datetime | None = None
    grant_id: str = ""

    def __post_init__(self) -> None:
        """Validate the grant's own consistency.

        Raises:
            ValueError: If the accounts are unset, the cap is not positive, or
                the window is empty.
        """
        if not self.payer_account_id or not self.spender_id:
            raise ValueError("a spend grant needs both a payer account and a spender")
        if self.max_milli_credits is not None and self.max_milli_credits <= 0:
            raise ValueError("max_milli_credits must be positive when set")
        if self.expires_at is not None and _as_utc(self.expires_at) <= _as_utc(
            self.granted_at
        ):
            raise ValueError("expires_at must be after granted_at")

    def is_valid_at(self, moment: dt.datetime) -> bool:
        """Report whether the grant is in force at `moment`.

        Revocation is inclusive, so a grant is dead at the instant it is
        revoked; expiry is exclusive of the expiry instant for the same reason.
        """
        if self.revoked_at is not None and _as_utc(moment) >= _as_utc(self.revoked_at):
            return False
        if _as_utc(moment) < _as_utc(self.granted_at):
            return False
        return not (
            self.expires_at is not None and _as_utc(moment) >= _as_utc(self.expires_at)
        )


@dataclass
class IdentityRegistry:
    """Authoritative mapping from key ID to identity, plus delegation history.

    The registry keeps revoked keys rather than deleting them, so a receipt
    signed before revocation still verifies against a key whose history is
    intact, while a receipt signed after revocation does not.
    """

    _keys: dict[str, IdentityKey] = field(default_factory=dict)
    _delegations: dict[str, KeyDelegation] = field(default_factory=dict)
    _device_owners: dict[str, str] = field(default_factory=dict)
    _spend_grants: dict[str, SpendGrant] = field(default_factory=dict)

    def grant_spend_authority(
        self,
        payer_account_id: str,
        spender_id: str,
        granted_at: dt.datetime,
        max_milli_credits: int | None = None,
        expires_at: dt.datetime | None = None,
    ) -> SpendGrant:
        """Authorize `spender_id` to charge `payer_account_id`.

        A pair has at most one live grant, keyed by the account pair itself so
        the grant can always be found by the two identities it concerns. Issuing
        a replacement is therefore revoke-then-grant: the previous grant is
        superseded rather than stacked, which keeps "what may X spend from Y?"
        a single answer.

        Args:
            payer_account_id: The account whose funds may be spent.
            spender_id: The identity permitted to sign authorizations for it.
            granted_at: When the permission takes effect.
            max_milli_credits: Optional cap on a single authorization.
            expires_at: Optional expiry.

        Returns:
            The recorded grant.
        """
        key = f"{payer_account_id}<-{spender_id}"
        grant = SpendGrant(
            payer_account_id=payer_account_id,
            spender_id=spender_id,
            granted_at=granted_at,
            max_milli_credits=max_milli_credits,
            expires_at=expires_at,
            grant_id=key,
        )
        self._spend_grants[key] = grant
        return grant

    def revoke_spend_authority(
        self, payer_account_id: str, spender_id: str, revoked_at: dt.datetime
    ) -> None:
        """Withdraw a cross-account spend permission.

        Raises:
            UnknownKeyError: If no such grant exists.
        """
        key = f"{payer_account_id}<-{spender_id}"
        grant = self._spend_grants.get(key)
        if grant is None:
            raise UnknownKeyError(
                f"no spend grant from {payer_account_id} to {spender_id} to revoke"
            )
        self._spend_grants[key] = replace(grant, revoked_at=_as_utc(revoked_at))

    def check_spend_authority(
        self,
        spender_id: str,
        payer_account_id: str,
        amount: int,
        moment: dt.datetime,
    ) -> None:
        """Verify a spender may commit an account to `amount` at `moment`.

        Self-spending is always allowed: an identity may draw on its own
        account. Anything else requires a live grant that covers the amount.

        `amount` is the ceiling the spender is asking to commit — the
        authorization's budget cap, not the eventual bill. A grant therefore
        bounds what its holder may *authorize*, which is stricter than bounding
        what they end up charging: a holder of a 5,000 grant cannot sign a
        20,000 authorization and then rely on the usage coming in low.

        Args:
            spender_id: The identity that signed the authorization.
            payer_account_id: The account the authorization charges.
            amount: Milli-credits the authorization commits the account to.
            moment: The instant settlement is attempted.

        Raises:
            IdentityBindingError: If the spender is neither the account owner nor
                a live grantee, or the grant does not cover the amount.
        """
        if spender_id == payer_account_id:
            return
        grant = self._spend_grants.get(f"{payer_account_id}<-{spender_id}")
        if grant is None:
            raise IdentityBindingError(
                f"{spender_id} has no authority to spend {payer_account_id}; a valid "
                "signature proves who signed, not whose funds may be used, so "
                "cross-account spending requires an explicit grant"
            )
        if not grant.is_valid_at(moment):
            raise IdentityBindingError(
                f"the spend grant from {payer_account_id} to {spender_id} is not in "
                f"force at {_as_utc(moment).isoformat()}"
            )
        if grant.max_milli_credits is not None and amount > grant.max_milli_credits:
            raise IdentityBindingError(
                f"the spend grant from {payer_account_id} to {spender_id} caps a "
                f"charge at {grant.max_milli_credits} milli-credits, got {amount}"
            )

    def register_key(
        self,
        key_id: str,
        owner_id: str,
        purpose: KeyPurpose,
        public_key_b64: str,
        registered_at: dt.datetime | None = None,
    ) -> IdentityKey:
        """Register a public key for an identity.

        Args:
            key_id: Stable identifier used in signed records.
            owner_id: The consumer, device, or platform account it speaks for.
            purpose: Which signing role this key fills.
            public_key_b64: Base64 Ed25519 public key.
            registered_at: Registration time; defaults to now.

        Returns:
            The stored key record.

        Raises:
            IdentityBindingError: If the key ID is already registered to a
                different owner or purpose, or to the same owner and purpose
                with *different* key material. Re-registering the identical
                binding is a no-op; a differing public key is refused rather
                than silently ignored, because ignoring it would leave the
                registry and the durable store holding two different keys under
                one id.
        """
        existing = self._keys.get(key_id)
        moment = registered_at or _utc_now()
        if existing is not None:
            if (
                existing.owner_id == owner_id
                and existing.purpose == purpose
                and existing.public_key_b64 == public_key_b64
            ):
                return existing
            raise IdentityBindingError(
                f"key {key_id} is already bound to {existing.owner_id}/{existing.purpose} "
                f"with a different binding; refusing to rebind to {owner_id}/{purpose}"
            )
        key = IdentityKey(
            key_id=key_id,
            owner_id=owner_id,
            purpose=purpose,
            public_key_b64=public_key_b64,
            registered_at=_as_utc(moment),
        )
        # Fail early on malformed key material rather than at first signature.
        key.public_key()
        self._keys[key_id] = key
        return key

    def revoke_key(self, key_id: str, revoked_at: dt.datetime | None = None) -> None:
        """Revoke a key, keeping it resolvable for historical verification.

        Raises:
            UnknownKeyError: If the key was never registered.
        """
        key = self._require(key_id)
        self._keys[key_id] = IdentityKey(
            key_id=key.key_id,
            owner_id=key.owner_id,
            purpose=key.purpose,
            public_key_b64=key.public_key_b64,
            registered_at=key.registered_at,
            status="revoked",
            revoked_at=_as_utc(revoked_at or _utc_now()),
        )

    def add_delegation(self, delegation: KeyDelegation) -> KeyDelegation:
        """Record a delegation and return it.

        Raises:
            IdentityBindingError: If the delegatee key is unknown or has a
                different purpose than the delegation claims.
        """
        delegatee = self._require(delegation.delegatee_key_id)
        if delegatee.purpose != delegation.purpose:
            raise IdentityBindingError(
                f"delegation {delegation.delegation_id} claims purpose {delegation.purpose} "
                f"but key {delegatee.key_id} is registered as {delegatee.purpose}"
            )
        self._delegations[delegation.delegation_id] = delegation
        return delegation

    def revoke_delegation(self, delegation_id: str, revoked_at: dt.datetime | None = None) -> None:
        """Revoke a delegation.

        Raises:
            UnknownKeyError: If the delegation does not exist.
        """
        delegation = self._delegations.get(delegation_id)
        if delegation is None:
            raise UnknownKeyError(f"unknown delegation: {delegation_id}")
        self._delegations[delegation_id] = KeyDelegation(
            delegation_id=delegation.delegation_id,
            grantor_id=delegation.grantor_id,
            delegatee_key_id=delegation.delegatee_key_id,
            purpose=delegation.purpose,
            max_milli_credits=delegation.max_milli_credits,
            allowed_models=delegation.allowed_models,
            issued_at=delegation.issued_at,
            expires_at=delegation.expires_at,
            revoked_at=_as_utc(revoked_at or _utc_now()),
        )

    def delegations_for(self, key_id: str) -> list[KeyDelegation]:
        """Return every delegation issued to `key_id`, newest last."""
        return [d for d in self._delegations.values() if d.delegatee_key_id == key_id]

    def get_key(self, key_id: str) -> IdentityKey:
        """Return a registered key regardless of status.

        Raises:
            UnknownKeyError: If the key was never registered.
        """
        return self._require(key_id)

    def verify(
        self,
        key_id: str,
        owner_id: str,
        purpose: KeyPurpose,
        domain: str,
        payload: dict[str, Any],
        signature_b64: str,
        signed_at: dt.datetime | None = None,
    ) -> None:
        """Verify a signature against the registry's binding for a key.

        Args:
            key_id: The `key_id` claimed by the record.
            owner_id: The identity the record claims to speak for.
            purpose: The signing role the record requires.
            domain: Domain separation string.
            payload: The signed payload.
            signature_b64: The signature under test.
            signed_at: When the record was signed; defaults to now. Pass the
                record's own timestamp to verify historical validity.

        Raises:
            UnknownKeyError: If the key is not registered.
            IdentityBindingError: If owner or purpose do not match the binding.
            KeyRevokedError: If the key was revoked at or before `signed_at`.
            SignatureError: If the signature does not verify.
        """
        key = self._require(key_id)
        if key.owner_id != owner_id:
            raise IdentityBindingError(
                f"key {key_id} is bound to {key.owner_id!r}, but the record claims {owner_id!r}"
            )
        if key.purpose != purpose:
            raise IdentityBindingError(
                f"key {key_id} is registered for {key.purpose!r}, "
                f"but a {purpose!r} signature was required"
            )
        moment = _as_utc(signed_at or _utc_now())
        if not key.is_valid_at(moment):
            raise KeyRevokedError(
                f"key {key_id} was not valid at {moment.isoformat()} "
                f"(status={key.status}, revoked_at={key.revoked_at})"
            )
        if not verify_payload(domain, payload, signature_b64, key.public_key()):
            from .errors import SignatureError

            raise SignatureError(f"signature does not verify for key {key_id} under {domain}")

    def verify_delegated(
        self,
        key_id: str,
        owner_id: str,
        purpose: KeyPurpose,
        domain: str,
        payload: dict[str, Any],
        signature_b64: str,
        milli_credits: int,
        model: str | None,
        signed_at: dt.datetime,
    ) -> None:
        """Verify a signature produced under a delegated key.

        Args:
            key_id: The delegated key that signed.
            owner_id: The delegating identity on whose behalf it acted.
            purpose: The signing role.
            domain: Domain separation string.
            payload: The signed payload.
            signature_b64: The signature under test.
            milli_credits: Amount the record authorizes, checked against the cap.
            model: Model the record authorizes, checked against the allowlist.
            signed_at: When the record was signed.

        Raises:
            DelegationError: If no valid delegation covers this key, owner,
                amount and model at `signed_at`.
        """
        self.verify(key_id, owner_id, purpose, domain, payload, signature_b64, signed_at)
        delegations = [
            d
            for d in self.delegations_for(key_id)
            if d.grantor_id == owner_id and d.purpose == purpose and d.is_valid_at(signed_at)
        ]
        if not delegations:
            raise DelegationError(
                f"no valid delegation from {owner_id} to {key_id} at {signed_at.isoformat()}"
            )
        for delegation in delegations:
            try:
                delegation.check_scope(milli_credits, model)
            except DelegationError:
                continue
            return
        # Every delegation was in force but none covered this spend: report the
        # tightest cap so the operator sees which limit bound.
        caps = [d.max_milli_credits for d in delegations if d.max_milli_credits is not None]
        if caps:
            raise DelegationError(
                f"delegated key {key_id} is capped at {min(caps)} milli-credits, "
                f"requested {milli_credits}"
            )
        raise DelegationError(
            f"delegated key {key_id} cannot authorize model {model!r} at {signed_at.isoformat()}"
        )

    def _require(self, key_id: str) -> IdentityKey:
        key = self._keys.get(key_id)
        if key is None:
            raise UnknownKeyError(
                f"key {key_id!r} is not in the trusted registry; "
                "a self-declared public key is never accepted"
            )
        return key

    def bind_device(self, device_id: str, provider_id: str) -> None:
        """Record which provider account a device may sign usage for.

        A device key proves "this device reported these numbers"; it does not by
        itself prove the earnings belong to the provider named in the report.
        Without this binding, any registered device could name a different
        provider in its payload and redirect credit.

        Args:
            device_id: The device holding the signing key.
            provider_id: The provider account that device may report for.

        Raises:
            IdentityBindingError: If the device is already bound to a different
                provider. Rebinding is a deliberate, audited operation, not
                something a report can do implicitly.
        """
        existing = self._device_owners.get(device_id)
        if existing is not None and existing != provider_id:
            raise IdentityBindingError(
                f"device {device_id} is bound to {existing}; "
                f"refusing to let it report for {provider_id}"
            )
        self._device_owners[device_id] = provider_id

    def register_device_key(
        self,
        key_id: str,
        device_id: str,
        provider_id: str,
        public_key_b64: str,
        registered_at: dt.datetime | None = None,
    ) -> IdentityKey:
        """Register a contribution device key and bind it to its provider.

        This is the production entry point for onboarding a contributor's
        device. Registering the key and binding the provider are one operation on
        purpose: a key that exists without a binding would let its holder name
        any provider in a usage report, and `check_device_binding` refuses
        unbound devices, so a split registration would deadlock settlement.

        Args:
            key_id: Stable identifier used in signed records.
            device_id: The device holding the key.
            provider_id: The provider account whose usage this device may report.
            public_key_b64: Base64 Ed25519 public key.
            registered_at: Registration time; defaults to now.

        Returns:
            The stored key record.

        Raises:
            IdentityBindingError: If the key is already bound to another
                identity, or the device is already bound to another provider.
        """
        key = self.register_key(
            key_id=key_id,
            owner_id=device_id,
            purpose="provider_device",
            public_key_b64=public_key_b64,
            registered_at=registered_at,
        )
        self.bind_device(device_id, provider_id)
        return key

    def check_device_binding(self, device_id: str, provider_id: str) -> None:
        """Verify a device is bound to the provider it is reporting for.

        Args:
            device_id: The device that signed.
            provider_id: The provider named in the report.

        Raises:
            IdentityBindingError: If no binding exists or it names a different
                provider. An unbound device is refused rather than allowed,
                since a permissive default here would let any registered device
                claim any provider's revenue.
        """
        bound = self._device_owners.get(device_id)
        if bound is None:
            raise IdentityBindingError(
                f"device {device_id} has no registered provider binding; "
                "call bind_device before accepting usage it reports"
            )
        if bound != provider_id:
            raise IdentityBindingError(
                f"device {device_id} is bound to {bound}, but the report claims {provider_id}"
            )


def generate_key_pair() -> tuple[str, str]:
    """Generate an Ed25519 key pair as base64 strings.

    Returns:
        Tuple of (public_key_b64, private_key_b64). The private key is encoded
        for storage in a local key file; it never leaves the device.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    return base64_encode(
        private.public_key().public_bytes_raw()
    ), base64_encode(private.private_bytes_raw())


def load_private_key(private_key_b64: str) -> Any:
    """Load an Ed25519 private key from base64.

    Raises:
        UnknownKeyError: If the bytes are not a 32-byte Ed25519 private key.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw = base64_decode(private_key_b64)
    if len(raw) != 32:
        raise UnknownKeyError(f"Ed25519 private keys are 32 bytes, got {len(raw)}")
    return Ed25519PrivateKey.from_private_bytes(raw)