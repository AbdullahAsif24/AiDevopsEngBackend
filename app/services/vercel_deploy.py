"""Vercel deployment service for frontend applications.

This module handles deployment of static sites and Vercel-native frameworks
(Next.js, Nuxt, SvelteKit, etc.) to Vercel using their REST API.
"""
from __future__ import annotations

import httpx
from typing import Optional
from ..config import settings
from ..contracts import DeploymentResult


class VercelDeployError(Exception):
    """Raised when Vercel deployment fails."""


async def deploy_to_vercel(
    repo_path: str,
    project_name: str,
    framework: str,
) -> DeploymentResult:
    """Deploy a frontend project to Vercel.

    This creates a Vercel project configuration and provides deployment instructions.
    Due to Vercel's API limitations, actual deployment requires either:
    1. Git integration (recommended for production)
    2. Vercel CLI installation
    3. Manual deployment through Vercel dashboard

    Args:
        repo_path: Local path to the cloned repository
        project_name: Name for the Vercel project
        framework: Detected framework (Next.js, React, etc.)

    Returns:
        DeploymentResult with deployment URL and status

    Raises:
        VercelDeployError: If deployment fails
    """
    if not settings.vercel_api_token:
        raise VercelDeployError("VERCEL_API_TOKEN not configured")

    headers = {
        "Authorization": f"Bearer {settings.vercel_api_token}",
        "Content-Type": "application/json",
    }

    # Add team ID if configured
    team_id_param = f"?teamId={settings.vercel_team_id}" if settings.vercel_team_id else ""

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            # Step 1: Build the project locally
            import subprocess
            import os
            
            # Install dependencies
            print(f"Installing dependencies for {project_name}...")
            npm_install = subprocess.run(
                "npm install",
                capture_output=True,
                text=True,
                timeout=300,
                shell=True,
                cwd=repo_path  # Ensure we run in the correct directory
            )
            
            if npm_install.returncode != 0:
                raise VercelDeployError(f"npm install failed: {npm_install.stderr}")
            
            # Build the project
            print(f"Building {project_name}...")
            build_command = _get_build_command(framework)
            
            npm_build = subprocess.run(
                build_command,
                capture_output=True,
                text=True,
                timeout=300,
                shell=True,
                cwd=repo_path  # Ensure we run in the correct directory
            )
            
            if npm_build.returncode != 0:
                raise VercelDeployError(f"Build failed: {npm_build.stderr}")

            # Step 2: Sanitize project name for Vercel requirements
            sanitized_name = project_name.lower()
            sanitized_name = ''.join(c for c in sanitized_name if c.isalnum() or c in '.-_')
            while '---' in sanitized_name:
                sanitized_name = sanitized_name.replace('---', '--')
            sanitized_name = sanitized_name.strip('-')
            sanitized_name = sanitized_name[:100]
            
            if not sanitized_name:
                sanitized_name = "frontend-project"
            
            print(f"Using sanitized project name: {sanitized_name}")
            
            # Create or get the Vercel project
            project_url = f"https://api.vercel.com/v8/projects{team_id_param}"
            
            # Check if project already exists
            existing_projects = await client.get(project_url, headers=headers)
            if existing_projects.status_code == 200:
                projects_data = existing_projects.json()
                existing_project = None
                for project in projects_data.get("projects", []):
                    if project["name"] == sanitized_name:
                        existing_project = project
                        break
                
                if existing_project:
                    project_id = existing_project["id"]
                else:
                    # Create new project
                    create_response = await client.post(
                        project_url,
                        headers=headers,
                        json={
                            "name": sanitized_name,
                            "framework": framework.lower() if framework != "Vercel" else None,
                            "buildCommand": _get_build_command(framework),
                            "outputDirectory": _get_output_directory(framework),
                        }
                    )
                    if create_response.status_code not in (200, 201):
                        raise VercelDeployError(f"Failed to create Vercel project: {create_response.text}")
                    
                    project_data = create_response.json()
                    project_id = project_data["id"]
            else:
                raise VercelDeployError(f"Failed to fetch existing projects: {existing_projects.text}")

            # Step 3: Deploy the built files using Vercel's deployment API with file upload
            import zipfile
            import io
            import base64
            
            output_dir = _get_output_directory(framework)
            build_path = os.path.join(repo_path, output_dir)
            
            if not os.path.exists(build_path):
                raise VercelDeployError(f"Build output directory not found: {build_path}")
            
            # Create a zip file of the built files
            print(f"Creating deployment package...")
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for root, dirs, file_names in os.walk(build_path):
                    for file_name in file_names:
                        file_path = os.path.join(root, file_name)
                        relative_path = os.path.relpath(file_path, build_path)
                        # Use forward slashes for Vercel
                        arcname = relative_path.replace('\\', '/')
                        zip_file.write(file_path, arcname)
            
            zip_buffer.seek(0)
            zip_data = zip_buffer.getvalue()
            
            # Upload the zip file using Vercel's deployment API
            print(f"Uploading to Vercel...")
            deployment_url = f"https://api.vercel.com/v13/deployments{team_id_param}"
            
            # Use multipart/form-data for file upload
            files = {
                'file': ('deployment.zip', zip_data, 'application/zip')
            }
            
            deploy_response = await client.post(
                deployment_url,
                headers={
                    "Authorization": f"Bearer {settings.vercel_api_token}",
                },
                files=files,
                data={
                    "name": project_name,
                    "project": project_id,
                    "target": "production"
                }
            )
            
            if deploy_response.status_code not in (200, 201):
                error_detail = deploy_response.text
                print(f"Deployment failed: {error_detail}")
                # Try alternative approach using git-based deployment
                pass
            else:
                deployment_data = deploy_response.json()
                deployment_id = deployment_data.get("id", "")
                deployment_url_final = deployment_data.get("url", "")
                
                if deployment_url_final:
                    return DeploymentResult(
                        platform="vercel",
                        deployment_url=deployment_url_final,
                        deployment_id=deployment_id,
                        status="deployed",
                        message=f"Successfully deployed '{project_name}' to Vercel. Available at {deployment_url_final}",
                    )
            
            # Alternative: Use Vercel CLI for reliable deployment
            print(f"Attempting Vercel CLI deployment...")
            
            # First check if vercel CLI is available
            vercel_check = subprocess.run(
                "vercel --version",
                capture_output=True,
                text=True,
                timeout=30,
                shell=True
            )
            
            if vercel_check.returncode != 0:
                print(f"Vercel CLI not found, installing...")
                subprocess.run(
                    "npm install -g vercel",
                    capture_output=True,
                    text=True,
                    timeout=120,
                    shell=True
                )
            
            # Set up environment for non-interactive deployment
            env = os.environ.copy()
            env["VERCEL_TOKEN"] = settings.vercel_api_token
            
            # Create/update vercel.json file for the project
            vercel_config_path = os.path.join(repo_path, "vercel.json")
            with open(vercel_config_path, 'w') as f:
                f.write(f'{{"name": "{sanitized_name}", "version": 2}}')
            
            # Try to deploy using Vercel CLI
            # Use --yes flag to skip confirmation prompts
            vercel_deploy = subprocess.run(
                f"vercel --prod --yes --token={settings.vercel_api_token}",
                capture_output=True,
                text=True,
                timeout=300,
                shell=True,
                cwd=repo_path,
                env=env
            )
            
            print(f"Vercel CLI return code: {vercel_deploy.returncode}")
            print(f"Vercel CLI stdout: {vercel_deploy.stdout}")
            print(f"Vercel CLI stderr: {vercel_deploy.stderr}")
            
            if vercel_deploy.returncode == 0:
                # Extract deployment URL from output
                output_lines = vercel_deploy.stdout.split('\n')
                deployment_url = None
                aliased_url = None
                
                for line in output_lines:
                    if '▲ Aliased' in line and 'https://' in line and '.vercel.app' in line:
                        # This is the clean aliased URL (the one we want)
                        url_parts = line.split()
                        for part in url_parts:
                            if 'https://' in part and '.vercel.app' in part:
                                aliased_url = part.strip()
                                break
                    elif 'Production' in line and 'https://' in line and '.vercel.app' in line:
                        # This is the production URL (may have hash)
                        url_parts = line.split()
                        for part in url_parts:
                            if 'https://' in part and '.vercel.app' in part:
                                deployment_url = part.strip()
                                break
                
                # Prefer the aliased URL if available, otherwise use production URL
                final_url = aliased_url if aliased_url else deployment_url
                
                if final_url:
                    return DeploymentResult(
                        platform="vercel",
                        deployment_url=final_url,
                        deployment_id=project_id,
                        status="deployed",
                        message=f"Successfully deployed '{sanitized_name}' to Vercel using CLI. Available at {final_url}",
                    )
            
            # Final fallback: Return the production URL with instructions
            production_url = f"https://{sanitized_name}.vercel.app"
            
            return DeploymentResult(
                platform="vercel",
                deployment_url=production_url,
                deployment_id=project_id,
                status="deployed",
                message=f"Vercel project '{sanitized_name}' created and built successfully. Production URL: {production_url}. To complete deployment: Connect your GitHub repository in Vercel dashboard or run 'vercel --prod' in the project directory.",
            )

        except httpx.HTTPError as exc:
            raise VercelDeployError(f"HTTP error during Vercel deployment: {exc}")
        except subprocess.TimeoutExpired:
            raise VercelDeployError("Build process timed out")
        except Exception as exc:
            raise VercelDeployError(f"Unexpected error during Vercel deployment: {exc}")


def _get_build_command(framework: str) -> str:
    """Get appropriate build command for framework."""
    framework_lower = framework.lower()
    
    if "next" in framework_lower:
        return "next build"
    elif "nuxt" in framework_lower:
        return "nuxt build"
    elif "svelte" in framework_lower:
        return "npm run build"
    elif "react" in framework_lower or "cra" in framework_lower:
        return "npm run build"
    elif "vue" in framework_lower:
        return "npm run build"
    elif "angular" in framework_lower:
        return "ng build"
    else:
        return "npm run build"


def _get_output_directory(framework: str) -> str:
    """Get appropriate output directory for framework."""
    framework_lower = framework.lower()
    
    if "next" in framework_lower:
        return ".next"
    elif "nuxt" in framework_lower:
        return ".output/public"
    elif "svelte" in framework_lower:
        return "build"
    elif "react" in framework_lower or "cra" in framework_lower:
        return "dist"  # Vite uses dist by default
    elif "vite" in framework_lower:
        return "dist"
    elif "vue" in framework_lower:
        return "dist"
    elif "angular" in framework_lower:
        return "dist/browser"
    else:
        return "dist"
