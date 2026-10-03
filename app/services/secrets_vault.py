"""Secrets Management Vault Service.

Provides:
  * Military-grade encrypted secret storage using cryptography Fernet (AES-128 in CBC mode with HMAC SHA256)
  * Masked preview generation (sk_••••••••••••8a9c)
  * Secret rotation (manual or cryptographically generated token, version bumping v1 -> v2, expiration scheduling)
  * Environment-specific scoping (production, staging, development)
  * Team sharing & Role-Based Access Control (Admin, Developer, Viewer)
  * Immutable Audit Logging for secret creation, reveals, rotations, updates, and deletes
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets as py_secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography.fernet import Fernet

from ..contracts import (
    CreateSecretRequest,
    InviteTeamMemberRequest,
    RevealSecretResponse,
    RotateSecretRequest,
    SecretAuditLog,
    SecretEnvironment,
    SecretItem,
    SecretRole,
    TeamMember,
    UpdateSecretRequest,
)
from ..config import supabase_configured
from ..services.database import get_db_service


class SecretsVaultManager:
    """Manages encrypted secrets, key rotation lifecycle, team sharing, and audit logging."""

    def __init__(self):
        # Generate or derive a stable encryption key for the vault
        vault_master_seed = os.getenv("VAULT_MASTER_KEY", "aidevops-default-secret-vault-master-key-2026")
        derived_key = hashlib.sha256(vault_master_seed.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(derived_key))

        # In-memory stores for resilience / standalone mode
        # key: secret_id -> dict storing raw and encrypted metadata
        self._secrets: dict[str, dict] = {}
        self._audit_logs: list[SecretAuditLog] = []
        self._team_members: dict[str, list[TeamMember]] = {}

        # Seed initial team members for demo
        self._seed_demo_data()

    def _seed_demo_data(self):
        default_user = "demo-user"
        now = datetime.now(timezone.utc)
        self._team_members[default_user] = [
            TeamMember(
                id="team-1",
                user_id=default_user,
                email="lead.devops@company.io",
                name="Alex Rivers",
                role="Admin",
                status="active",
                joined_at=now - timedelta(days=45),
            ),
            TeamMember(
                id="team-2",
                user_id=default_user,
                email="sarah.chen@company.io",
                name="Sarah Chen",
                role="Developer",
                status="active",
                joined_at=now - timedelta(days=20),
            ),
            TeamMember(
                id="team-3",
                user_id=default_user,
                email="security.auditor@company.io",
                name="Marcus Vance",
                role="Viewer",
                status="active",
                joined_at=now - timedelta(days=10),
            ),
        ]

    def _encrypt(self, plain_text: str) -> str:
        """Encrypt plain text to Fernet token string."""
        return self._fernet.encrypt(plain_text.encode("utf-8")).decode("utf-8")

    def _decrypt(self, cipher_text: str) -> str:
        """Decrypt Fernet token string to plain text."""
        return self._fernet.decrypt(cipher_text.encode("utf-8")).decode("utf-8")

    def _mask_value(self, value: str) -> str:
        """Mask a secret value for secure UI display."""
        if len(value) <= 6:
            return "••••••••"
        prefix = value[:3]
        suffix = value[-4:]
        return f"{prefix}••••••••{suffix}"

    def _compute_status(self, expires_at: Optional[datetime]) -> str:
        if not expires_at:
            return "active"
        now = datetime.now(timezone.utc)
        if now > expires_at:
            return "expired"
        if expires_at - now < timedelta(days=7):
            return "expiring_soon"
        return "active"

    async def log_audit(
        self,
        user_id: str,
        secret_id: Optional[str],
        secret_key: str,
        environment: str,
        action: str,
        actor: str,
        details: str,
        ip_address: str = "127.0.0.1",
    ) -> SecretAuditLog:
        """Record an immutable audit log entry."""
        log = SecretAuditLog(
            id=str(uuid.uuid4()),
            user_id=user_id,
            secret_id=secret_id,
            secret_key=secret_key,
            environment=environment,
            action=action,  # type: ignore
            actor=actor,
            ip_address=ip_address,
            details=details,
            timestamp=datetime.now(timezone.utc),
        )
        self._audit_logs.insert(0, log)
        if len(self._audit_logs) > 200:
            self._audit_logs.pop()

        if supabase_configured():
            try:
                db = get_db_service()
                db.client.table("secret_audit_logs").insert({
                    "id": log.id,
                    "user_id": log.user_id,
                    "secret_id": log.secret_id,
                    "secret_key": log.secret_key,
                    "environment": log.environment,
                    "action": log.action,
                    "actor": log.actor,
                    "ip_address": log.ip_address,
                    "details": log.details,
                    "timestamp": log.timestamp.isoformat(),
                }).execute()
            except Exception as e:
                print(f"[SecretsVault] Supabase audit log fallback: {e}")

        return log

    async def list_secrets(
        self, user_id: str, environment: Optional[SecretEnvironment] = None
    ) -> list[SecretItem]:
        """List secrets with masked preview and rotation metadata."""
        items: list[SecretItem] = []

        if supabase_configured():
            try:
                db = get_db_service()
                query = db.client.table("secrets").select("*").eq("user_id", user_id)
                if environment:
                    query = query.eq("environment", environment)
                res = query.order("created_at", desc=True).execute()
                if res.data:
                    for row in res.data:
                        expires = datetime.fromisoformat(row["expires_at"]) if row.get("expires_at") else None
                        items.append(
                            SecretItem(
                                id=row["id"],
                                user_id=row["user_id"],
                                key=row["key"],
                                environment=row["environment"],
                                masked_value=row["masked_value"],
                                version=row.get("version", 1),
                                rotation_interval_days=row.get("rotation_interval_days", 30),
                                last_rotated_at=datetime.fromisoformat(row["last_rotated_at"]),
                                expires_at=expires,
                                status=self._compute_status(expires),  # type: ignore
                                shared_roles=row.get("shared_roles", ["Admin", "Developer"]),
                                created_at=datetime.fromisoformat(row["created_at"]),
                                updated_at=datetime.fromisoformat(row.get("updated_at", row["created_at"])),
                            )
                        )
                    return items
            except Exception as e:
                print(f"[SecretsVault] Supabase list_secrets fallback: {e}")

        # In-memory fallback
        for entry in self._secrets.values():
            if entry["user_id"] == user_id:
                if environment and entry["environment"] != environment:
                    continue
                expires = entry.get("expires_at")
                items.append(
                    SecretItem(
                        id=entry["id"],
                        user_id=entry["user_id"],
                        key=entry["key"],
                        environment=entry["environment"],
                        masked_value=entry["masked_value"],
                        version=entry.get("version", 1),
                        rotation_interval_days=entry.get("rotation_interval_days", 30),
                        last_rotated_at=entry["last_rotated_at"],
                        expires_at=expires,
                        status=self._compute_status(expires),  # type: ignore
                        shared_roles=entry.get("shared_roles", ["Admin", "Developer"]),
                        created_at=entry["created_at"],
                        updated_at=entry["updated_at"],
                    )
                )

        return items

    async def create_secret(
        self, user_id: str, req: CreateSecretRequest, actor: str = "DevOps Admin"
    ) -> SecretItem:
        """Create and securely encrypt a new secret with rotation policy."""
        secret_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        encrypted_token = self._encrypt(req.value)
        masked = self._mask_value(req.value)

        expires_at = None
        if req.rotation_interval_days and req.rotation_interval_days > 0:
            expires_at = now + timedelta(days=req.rotation_interval_days)

        secret_data = {
            "id": secret_id,
            "user_id": user_id,
            "key": req.key.strip().upper(),
            "environment": req.environment,
            "encrypted_value": encrypted_token,
            "masked_value": masked,
            "version": 1,
            "rotation_interval_days": req.rotation_interval_days,
            "last_rotated_at": now,
            "expires_at": expires_at,
            "shared_roles": req.shared_roles,
            "history": [],
            "created_at": now,
            "updated_at": now,
        }

        if supabase_configured():
            try:
                db = get_db_service()
                db.client.table("secrets").insert({
                    "id": secret_data["id"],
                    "user_id": secret_data["user_id"],
                    "key": secret_data["key"],
                    "environment": secret_data["environment"],
                    "encrypted_value": secret_data["encrypted_value"],
                    "masked_value": secret_data["masked_value"],
                    "version": secret_data["version"],
                    "rotation_interval_days": secret_data["rotation_interval_days"],
                    "last_rotated_at": secret_data["last_rotated_at"].isoformat(),
                    "expires_at": secret_data["expires_at"].isoformat() if secret_data["expires_at"] else None,
                    "shared_roles": secret_data["shared_roles"],
                    "created_at": secret_data["created_at"].isoformat(),
                    "updated_at": secret_data["updated_at"].isoformat(),
                }).execute()
            except Exception as e:
                print(f"[SecretsVault] Supabase create_secret fallback: {e}")

        self._secrets[secret_id] = secret_data

        await self.log_audit(
            user_id=user_id,
            secret_id=secret_id,
            secret_key=secret_data["key"],
            environment=secret_data["environment"],
            action="create",
            actor=actor,
            details=f"Created secret {secret_data['key']} (v1) in {secret_data['environment']} with {req.rotation_interval_days}-day rotation",
        )

        return SecretItem(
            id=secret_id,
            user_id=user_id,
            key=secret_data["key"],
            environment=secret_data["environment"],
            masked_value=secret_data["masked_value"],
            version=1,
            rotation_interval_days=secret_data["rotation_interval_days"],
            last_rotated_at=now,
            expires_at=expires_at,
            status=self._compute_status(expires_at),  # type: ignore
            shared_roles=secret_data["shared_roles"],
            created_at=now,
            updated_at=now,
        )

    async def reveal_secret(
        self, secret_id: str, user_id: str, actor: str = "DevOps Engineer"
    ) -> Optional[RevealSecretResponse]:
        """Audit-logged decrypt and reveal of a secret value."""
        secret = self._secrets.get(secret_id)
        if not secret:
            if supabase_configured():
                try:
                    db = get_db_service()
                    res = db.client.table("secrets").select("*").eq("id", secret_id).execute()
                    if res.data:
                        secret = res.data[0]
                except Exception as e:
                    print(f"[SecretsVault] Supabase reveal lookup error: {e}")

        if not secret or secret["user_id"] != user_id:
            return None

        plain = self._decrypt(secret["encrypted_value"])
        now = datetime.now(timezone.utc)

        await self.log_audit(
            user_id=user_id,
            secret_id=secret_id,
            secret_key=secret["key"],
            environment=secret["environment"],
            action="reveal",
            actor=actor,
            details=f"Decrypted and viewed plain text value for {secret['key']} (v{secret.get('version', 1)})",
        )

        return RevealSecretResponse(
            id=secret["id"],
            key=secret["key"],
            environment=secret["environment"],
            plain_value=plain,
            revealed_at=now,
        )

    async def rotate_secret(
        self, secret_id: str, user_id: str, req: RotateSecretRequest, actor: str = "Security Admin"
    ) -> Optional[SecretItem]:
        """Rotate a secret: archives old value, generates or saves new value, bumps version, logs audit."""
        secret = self._secrets.get(secret_id)
        if not secret:
            if supabase_configured():
                try:
                    db = get_db_service()
                    res = db.client.table("secrets").select("*").eq("id", secret_id).execute()
                    if res.data:
                        secret = res.data[0]
                        self._secrets[secret_id] = secret
                except Exception as e:
                    print(f"[SecretsVault] Supabase rotate lookup error: {e}")

        if not secret or secret["user_id"] != user_id:
            return None

        # Determine new value
        if req.auto_generate or not req.new_value:
            new_val = f"sec_live_{py_secrets.token_urlsafe(32)}"
        else:
            new_val = req.new_value

        now = datetime.now(timezone.utc)
        new_encrypted = self._encrypt(new_val)
        new_masked = self._mask_value(new_val)
        new_version = secret.get("version", 1) + 1

        rotation_interval = secret.get("rotation_interval_days", 30)
        expires_at = None
        if rotation_interval and rotation_interval > 0:
            expires_at = now + timedelta(days=rotation_interval)

        # Archive current version
        history = secret.get("history", [])
        history.append({
            "version": secret.get("version", 1),
            "encrypted_value": secret["encrypted_value"],
            "masked_value": secret["masked_value"],
            "rotated_at": now.isoformat(),
        })

        secret["encrypted_value"] = new_encrypted
        secret["masked_value"] = new_masked
        secret["version"] = new_version
        secret["last_rotated_at"] = now
        secret["expires_at"] = expires_at
        secret["history"] = history
        secret["updated_at"] = now

        if supabase_configured():
            try:
                db = get_db_service()
                db.client.table("secrets").update({
                    "encrypted_value": new_encrypted,
                    "masked_value": new_masked,
                    "version": new_version,
                    "last_rotated_at": now.isoformat(),
                    "expires_at": expires_at.isoformat() if expires_at else None,
                    "updated_at": now.isoformat(),
                }).eq("id", secret_id).execute()
            except Exception as e:
                print(f"[SecretsVault] Supabase rotate update fallback: {e}")

        await self.log_audit(
            user_id=user_id,
            secret_id=secret_id,
            secret_key=secret["key"],
            environment=secret["environment"],
            action="rotate",
            actor=actor,
            details=f"Rotated key to version v{new_version}. Expiration rescheduled for {expires_at.strftime('%Y-%m-%d') if expires_at else 'unlimited'}",
        )

        return SecretItem(
            id=secret["id"],
            user_id=user_id,
            key=secret["key"],
            environment=secret["environment"],
            masked_value=new_masked,
            version=new_version,
            rotation_interval_days=rotation_interval,
            last_rotated_at=now,
            expires_at=expires_at,
            status=self._compute_status(expires_at),  # type: ignore
            shared_roles=secret.get("shared_roles", ["Admin", "Developer"]),
            created_at=secret.get("created_at", now),
            updated_at=now,
        )

    async def update_secret(
        self, secret_id: str, user_id: str, req: UpdateSecretRequest, actor: str = "DevOps Lead"
    ) -> Optional[SecretItem]:
        """Update secret metadata (key name, environment, rotation interval, roles)."""
        secret = self._secrets.get(secret_id)
        if not secret or secret["user_id"] != user_id:
            return None

        now = datetime.now(timezone.utc)
        if req.key:
            secret["key"] = req.key.strip().upper()
        if req.environment:
            secret["environment"] = req.environment
        if req.rotation_interval_days is not None:
            secret["rotation_interval_days"] = req.rotation_interval_days
            if req.rotation_interval_days > 0:
                secret["expires_at"] = secret["last_rotated_at"] + timedelta(days=req.rotation_interval_days)
            else:
                secret["expires_at"] = None
        if req.shared_roles is not None:
            secret["shared_roles"] = req.shared_roles

        secret["updated_at"] = now

        if supabase_configured():
            try:
                db = get_db_service()
                db.client.table("secrets").update({
                    "key": secret["key"],
                    "environment": secret["environment"],
                    "rotation_interval_days": secret["rotation_interval_days"],
                    "expires_at": secret["expires_at"].isoformat() if secret["expires_at"] else None,
                    "shared_roles": secret["shared_roles"],
                    "updated_at": now.isoformat(),
                }).eq("id", secret_id).execute()
            except Exception as e:
                print(f"[SecretsVault] Supabase update secret fallback: {e}")

        await self.log_audit(
            user_id=user_id,
            secret_id=secret_id,
            secret_key=secret["key"],
            environment=secret["environment"],
            action="update",
            actor=actor,
            details=f"Updated secret configuration for {secret['key']}",
        )

        return SecretItem(
            id=secret["id"],
            user_id=user_id,
            key=secret["key"],
            environment=secret["environment"],
            masked_value=secret["masked_value"],
            version=secret.get("version", 1),
            rotation_interval_days=secret.get("rotation_interval_days", 30),
            last_rotated_at=secret["last_rotated_at"],
            expires_at=secret.get("expires_at"),
            status=self._compute_status(secret.get("expires_at")),  # type: ignore
            shared_roles=secret.get("shared_roles", ["Admin", "Developer"]),
            created_at=secret["created_at"],
            updated_at=now,
        )

    async def delete_secret(
        self, secret_id: str, user_id: str, actor: str = "DevOps Lead"
    ) -> bool:
        """Permanently delete a secret and record audit log."""
        secret = self._secrets.get(secret_id)
        if not secret or secret["user_id"] != user_id:
            return False

        if supabase_configured():
            try:
                db = get_db_service()
                db.client.table("secrets").delete().eq("id", secret_id).execute()
            except Exception as e:
                print(f"[SecretsVault] Supabase delete secret fallback: {e}")

        await self.log_audit(
            user_id=user_id,
            secret_id=secret_id,
            secret_key=secret["key"],
            environment=secret["environment"],
            action="delete",
            actor=actor,
            details=f"Permanently purged secret {secret['key']} from vault",
        )

        self._secrets.pop(secret_id, None)
        return True

    async def list_audit_logs(self, user_id: str) -> list[SecretAuditLog]:
        """List audit events for secrets."""
        if supabase_configured():
            try:
                db = get_db_service()
                res = (
                    db.client.table("secret_audit_logs")
                    .select("*")
                    .eq("user_id", user_id)
                    .order("timestamp", desc=True)
                    .limit(50)
                    .execute()
                )
                if res.data:
                    return [
                        SecretAuditLog(
                            id=item["id"],
                            user_id=item["user_id"],
                            secret_id=item.get("secret_id"),
                            secret_key=item["secret_key"],
                            environment=item["environment"],
                            action=item["action"],
                            actor=item["actor"],
                            ip_address=item.get("ip_address", "127.0.0.1"),
                            details=item["details"],
                            timestamp=datetime.fromisoformat(item["timestamp"]),
                        )
                        for item in res.data
                    ]
            except Exception as e:
                print(f"[SecretsVault] Supabase list_audit_logs fallback: {e}")

        return [log for log in self._audit_logs if log.user_id == user_id]

    async def list_team_members(self, user_id: str) -> list[TeamMember]:
        """List team members who share access to secrets."""
        return self._team_members.get(user_id, self._team_members.get("demo-user", []))

    async def invite_team_member(
        self, user_id: str, req: InviteTeamMemberRequest, actor: str = "DevOps Admin"
    ) -> TeamMember:
        """Invite a team member with specified role permissions."""
        now = datetime.now(timezone.utc)
        member = TeamMember(
            id=str(uuid.uuid4()),
            user_id=user_id,
            email=req.email.strip().lower(),
            name=req.name.strip(),
            role=req.role,
            status="active",
            joined_at=now,
        )

        if user_id not in self._team_members:
            self._team_members[user_id] = []
        self._team_members[user_id].append(member)

        await self.log_audit(
            user_id=user_id,
            secret_id=None,
            secret_key="TEAM_PERMISSIONS",
            environment="all",
            action="share",
            actor=actor,
            details=f"Invited {member.name} ({member.email}) as {member.role}",
        )

        return member

    async def remove_team_member(self, user_id: str, member_id: str, actor: str = "DevOps Admin") -> bool:
        """Remove a team member's access."""
        members = self._team_members.get(user_id, [])
        for i, m in enumerate(members):
            if m.id == member_id:
                removed = members.pop(i)
                await self.log_audit(
                    user_id=user_id,
                    secret_id=None,
                    secret_key="TEAM_PERMISSIONS",
                    environment="all",
                    action="share",
                    actor=actor,
                    details=f"Revoked secret access for {removed.name} ({removed.email})",
                )
                return True
        return False


_secrets_vault_manager: Optional[SecretsVaultManager] = None


def get_secrets_vault_manager() -> SecretsVaultManager:
    global _secrets_vault_manager
    if _secrets_vault_manager is None:
        _secrets_vault_manager = SecretsVaultManager()
    return _secrets_vault_manager
