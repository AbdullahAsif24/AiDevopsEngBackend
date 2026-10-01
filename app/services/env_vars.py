"""Environment variable management service."""
from __future__ import annotations

from typing import Any

from .database import get_db_service


class EnvVarManager:
    """Manage environment variables for deployments."""

    def __init__(self):
        """Initialize with database service."""
        self.db = get_db_service()

    async def store_env_vars(self, job_id: str, env_vars: dict[str, str]) -> None:
        """Store environment variables for a job.

        Args:
            job_id: The job identifier
            env_vars: Dictionary of environment variable key-value pairs
        """
        await self.db.store_env_vars(job_id, env_vars)

    async def get_env_vars(self, job_id: str) -> dict[str, str]:
        """Retrieve environment variables for a job.

        Args:
            job_id: The job identifier

        Returns:
            Dictionary of environment variable key-value pairs
        """
        return await self.db.get_env_vars(job_id)

    async def inject_into_deployment(
        self, deployment_config: dict[str, Any], env_vars: dict[str, str]
    ) -> dict[str, Any]:
        """Inject environment variables into deployment configuration.

        This method handles different deployment platforms' requirements for
        environment variables.

        Args:
            deployment_config: The base deployment configuration
            env_vars: Dictionary of environment variables to inject

        Returns:
            Updated deployment configuration with environment variables
        """
        platform = deployment_config.get("platform", "")

        if platform == "vercel":
            return self._inject_vercel_env_vars(deployment_config, env_vars)
        elif platform == "render":
            return self._inject_render_env_vars(deployment_config, env_vars)
        else:
            # For other platforms, add to a generic env section
            deployment_config["env"] = env_vars
            return deployment_config

    def _inject_vercel_env_vars(
        self, config: dict[str, Any], env_vars: dict[str, str]
    ) -> dict[str, Any]:
        """Inject environment variables for Vercel deployment.

        Vercel expects environment variables in the 'env' array format.
        """
        if "env" not in config:
            config["env"] = []

        for key, value in env_vars.items():
            config["env"].append(
                {
                    "key": key,
                    "value": value,
                    "type": "encrypted",  # Vercel recommends encrypted for sensitive data
                }
            )

        return config

    def _inject_render_env_vars(
        self, config: dict[str, Any], env_vars: dict[str, str]
    ) -> dict[str, Any]:
        """Inject environment variables for Render deployment.

        Render expects environment variables in the 'envVars' object format.
        """
        if "envVars" not in config:
            config["envVars"] = {}

        config["envVars"].update(env_vars)
        return config

    def validate_env_vars(self, env_vars: dict[str, str]) -> tuple[bool, list[str]]:
        """Validate environment variable keys and values.

        Args:
            env_vars: Dictionary of environment variables

        Returns:
            Tuple of (is_valid, error_messages)
        """
        errors = []

        for key, value in env_vars.items():
            # Validate key format
            if not key:
                errors.append("Environment variable key cannot be empty")
                continue

            if not isinstance(key, str):
                errors.append(f"Environment variable key must be string, got {type(key)}")
                continue

            # Check for invalid characters (letters, numbers, underscore only)
            if not key.replace("_", "").isalnum():
                errors.append(
                    f"Invalid environment variable key '{key}': only letters, numbers, and underscores allowed"
                )

            # Validate value
            if not isinstance(value, str):
                errors.append(
                    f"Environment variable value for '{key}' must be string, got {type(value)}"
                )

            # Check length limits (platform-specific limits vary, but use conservative limits)
            if len(key) > 100:
                errors.append(f"Environment variable key '{key}' exceeds 100 character limit")

            if len(value) > 10000:
                errors.append(
                    f"Environment variable value for '{key}' exceeds 10000 character limit"
                )

        return len(errors) == 0, errors


# Global env var manager instance
_env_var_manager: EnvVarManager | None = None


def get_env_var_manager() -> EnvVarManager:
    """Get or create the global environment variable manager instance."""
    global _env_var_manager
    if _env_var_manager is None:
        _env_var_manager = EnvVarManager()
    return _env_var_manager