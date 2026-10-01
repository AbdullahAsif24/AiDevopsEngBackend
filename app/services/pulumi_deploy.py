"""Pulumi-based deployment service for unified multi-platform deployments.

This service uses Pulumi to deploy to multiple platforms (Vercel, Render, Railway, etc.)
through a unified API, allowing users to connect their accounts and deploy seamlessly.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any, Optional
from pathlib import Path

from ..config import settings
from ..contracts import DeploymentResult


class PulumiDeployError(Exception):
    """Raised when Pulumi deployment fails."""


class PulumiDeployer:
    """Unified deployment service using Pulumi."""

    def __init__(self):
        """Initialize Pulumi deployer."""
        self.pulumi_available = self._check_pulumi()

    def _check_pulumi(self) -> bool:
        """Check if Pulumi is installed and available."""
        try:
            import pulumi
            return True
        except ImportError:
            print("Pulumi not installed. Install with: pip install pulumi")
            return False

    async def deploy(
        self,
        platform: str,
        repo_path: str,
        service_name: str,
        access_token: str,
        env_vars: Optional[dict] = None,
        repo_url: Optional[str] = None,
        dockerfile_content: Optional[str] = None,
    ) -> DeploymentResult:
        """Deploy to a platform using Pulumi.

        Args:
            platform: Target platform (vercel, render, railway, etc.)
            repo_path: Local path to the repository
            service_name: Name for the deployment
            access_token: User's access token for the platform
            env_vars: Optional environment variables
            repo_url: GitHub repository URL
            dockerfile_content: Optional Dockerfile content

        Returns:
            DeploymentResult with deployment URL and status

        Raises:
            PulumiDeployError: If deployment fails
        """
        if not self.pulumi_available:
            raise PulumiDeployError("Pulumi not installed")

        # Delegate to platform-specific deployer
        if platform == "vercel":
            return await self._deploy_vercel(
                repo_path, service_name, access_token, env_vars, repo_url
            )
        elif platform == "render":
            return await self._deploy_render(
                repo_path, service_name, access_token, env_vars, repo_url, dockerfile_content
            )
        elif platform == "railway":
            return await self._deploy_railway(
                repo_path, service_name, access_token, env_vars, repo_url
            )
        else:
            raise PulumiDeployError(f"Unsupported platform: {platform}")

    async def _deploy_vercel(
        self,
        repo_path: str,
        service_name: str,
        access_token: str,
        env_vars: Optional[dict],
        repo_url: Optional[str],
    ) -> DeploymentResult:
        """Deploy to Vercel using Pulumi.

        Note: Pulumi Vercel provider is in preview. For now, we use the existing
        direct API implementation but this is the structure for Pulumi integration.
        """
        # Import the existing Vercel deployer
        from .vercel_deploy import deploy_to_vercel

        # For now, use the existing implementation
        # TODO: Migrate to pure Pulumi when provider is stable
        from .deployment_detector import detect_deployment_type

        # Detect framework from repo
        detection = await detect_deployment_type(repo_path)
        framework = detection.detected_framework

        result = await deploy_to_vercel(
            repo_path=repo_path,
            project_name=service_name,
            framework=framework,
            access_token=access_token,
            env_vars=env_vars or {},
        )

        return result

    async def _deploy_render(
        self,
        repo_path: str,
        service_name: str,
        access_token: str,
        env_vars: Optional[dict],
        repo_url: Optional[str],
        dockerfile_content: Optional[str],
    ) -> DeploymentResult:
        """Deploy to Render using Pulumi.

        Note: Render doesn't have a native Pulumi provider yet. We use the
        existing direct API implementation.
        """
        from .render_deploy import deploy_to_render

        if not repo_url:
            raise PulumiDeployError("GitHub repo URL required for Render deployment")

        result = await deploy_to_render(
            repo_path=repo_path,
            service_name=service_name,
            dockerfile_content=dockerfile_content or "",
            access_token=access_token,
            env_vars=env_vars or {},
            repo_url=repo_url,
        )

        return result

    async def _deploy_railway(
        self,
        repo_path: str,
        service_name: str,
        access_token: str,
        env_vars: Optional[dict],
        repo_url: Optional[str],
    ) -> DeploymentResult:
        """Deploy to Railway using Pulumi or direct API.

        Railway has a REST API. We'll use direct API calls.
        """
        import httpx

        if not repo_url:
            raise PulumiDeployError("GitHub repo URL required for Railway deployment")

        # Railway API endpoint
        api_url = "https://backboard.railway.app/graphql/v2"
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        # Create a new project
        create_project_query = """
        mutation ($name: String!) {
            projectCreate(input: {name: $name}) {
                project {
                    id
                    name
                }
            }
        }
        """

        # Create a service in the project
        create_service_query = """
        mutation ($projectId: String!, $serviceId: String!) {
            serviceCreate(input: {projectId: $projectId, serviceId: $serviceId}) {
                service {
                    id
                    name
                }
            }
        }
        """

        # Connect GitHub repo
        connect_repo_query = """
        mutation ($serviceId: String!, $repoUrl: String!) {
            serviceRepoConnect(input: {serviceId: $serviceId, repoUrl: $repoUrl}) {
                service {
                    id
                }
            }
        }
        """

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Create project
            project_response = await client.post(
                api_url,
                headers=headers,
                json={"query": create_project_query, "variables": {"name": service_name}},
            )

            if project_response.status_code != 200:
                raise PulumiDeployError(f"Failed to create Railway project: {project_response.text}")

            project_data = project_response.json()
            project_id = project_data["data"]["projectCreate"]["project"]["id"]

            # Create service
            service_response = await client.post(
                api_url,
                headers=headers,
                json={
                    "query": create_service_query,
                    "variables": {"projectId": project_id, "serviceId": f"{service_name}-service"},
                },
            )

            if service_response.status_code != 200:
                raise PulumiDeployError(f"Failed to create Railway service: {service_response.text}")

            service_data = service_response.json()
            service_id = service_data["data"]["serviceCreate"]["service"]["id"]

            # Connect GitHub repo
            repo_response = await client.post(
                api_url,
                headers=headers,
                json={
                    "query": connect_repo_query,
                    "variables": {"serviceId": service_id, "repoUrl": repo_url},
                },
            )

            if repo_response.status_code != 200:
                raise PulumiDeployError(f"Failed to connect Railway repo: {repo_response.text}")

            # Railway typically deploys to: https://<project-name>.railway.app
            deployment_url = f"https://{service_name}.railway.app"

            return DeploymentResult(
                platform="railway",
                deployment_url=deployment_url,
                deployment_id=service_id,
                status="deployed",
                message=f"Successfully deployed to Railway. Live at {deployment_url}",
            )


def get_pulumi_deployer() -> PulumiDeployer:
    """Get or create the global Pulumi deployer instance."""
    return PulumiDeployer()
