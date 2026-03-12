"""
Code Container Session Service
------------------------------
Manages long-lived ACI containers for candidate interview coding sessions.
Container is created once per interview, kept alive with a sleep loop,
and destroyed when the candidate finishes the coding section.
"""

import uuid
import logging
import time
import asyncio
from typing import Optional
from app.core.config import settings

logger = logging.getLogger(__name__)

try:
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.containerinstance import ContainerInstanceManagementClient
    from azure.mgmt.containerinstance.models import (
        ContainerGroup,
        Container,
        ResourceRequests,
        ResourceRequirements,
        OperatingSystemTypes,
        ImageRegistryCredential,
    )
    AZURE_SDK_AVAILABLE = True
except ImportError:
    AZURE_SDK_AVAILABLE = False


class CodeContainerSessionService:
    @staticmethod
    def is_azure_aci_configured() -> bool:
        return bool(getattr(settings, "AZURE_SUBSCRIPTION_ID", None) and getattr(settings, "AZURE_ACI_RESOURCE_GROUP", None))

    @staticmethod
    def _get_client():
        if not AZURE_SDK_AVAILABLE:
            raise Exception("Azure SDK not available.")
        sub_id = settings.AZURE_SUBSCRIPTION_ID
        credential = DefaultAzureCredential()
        return ContainerInstanceManagementClient(credential, sub_id)

    @staticmethod
    def create_session_container(interview_id: str, language: str = None) -> bool:
        """
        Creates a long-lived container for the given interview_id.
        """
        if not CodeContainerSessionService.is_azure_aci_configured():
            logger.info("Azure ACI not configured, using local fallback.")
            return True # Fallback

        resource_group = settings.AZURE_ACI_RESOURCE_GROUP
        location = getattr(settings, "AZURE_ACI_LOCATION", "eastus")
        acr_server = getattr(settings, "AZURE_ACR_SERVER", "")
        acr_user = getattr(settings, "AZURE_ACR_USERNAME", None)
        acr_pass = getattr(settings, "AZURE_ACR_PASSWORD", None)

        lang_map = {
            "python3": "code-runner-python",
            "javascript": "code-runner-javascript",
            "java": "code-runner-java",
            "cpp": "code-runner-cpp",
        }
        image_name = lang_map.get(language, "code-runner-python")
        image_base = f"{acr_server}/{image_name}:latest" if acr_server else f"{image_name}:latest"
        image = image_base

        cg_name = f"code-runner-{interview_id}"
        container_name = "runner"

        client = CodeContainerSessionService._get_client()

        # Check if container already exists
        try:
            existing = client.container_groups.get(resource_group, cg_name)
            state = existing.instance_view.state if existing.instance_view else "Unknown"
            if state == "Running":
                logger.info(f"Container {cg_name} already running.")
                return True
        except Exception:
            pass # Does not exist

        container_resource = Container(
            name=container_name,
            image=image,
            resources=ResourceRequirements(requests=ResourceRequests(memory_in_gb=1.0, cpu=1.0)),
            command=["timeout", "45m", "bash", "-c", "while true; do sleep 60; done"],
        )

        import datetime
        now_ts = str(datetime.datetime.now(datetime.timezone.utc).timestamp())
        
        registry_credentials = []
        if acr_server and acr_user and acr_pass:
            registry_credentials.append(
                ImageRegistryCredential(
                    server=acr_server,
                    username=acr_user,
                    password=acr_pass
                )
            )

        group = ContainerGroup(
            location=location,
            containers=[container_resource],
            os_type=OperatingSystemTypes.linux,
            restart_policy="Never",
            tags={"creationTime": now_ts},
            image_registry_credentials=registry_credentials if registry_credentials else None
        )

        logger.info(f"Creating session container {cg_name}")
        poller = client.container_groups.begin_create_or_update(resource_group, cg_name, group)
        poller.result() # Wait until created

        # Wait up to 60 seconds for Running state
        max_waits = 30
        for _ in range(max_waits):
            cg = client.container_groups.get(resource_group, cg_name)
            state = cg.instance_view.state if cg.instance_view else "Unknown"
            if state == "Running":
                return True
            time.sleep(2)

        raise Exception("Timeout waiting for container to be Running state")

    @staticmethod
    def get_session_container(interview_id: str):
        if not CodeContainerSessionService.is_azure_aci_configured():
            return None

        cg_name = f"code-runner-{interview_id}"
        client = CodeContainerSessionService._get_client()
        resource_group = settings.AZURE_ACI_RESOURCE_GROUP
        try:
            return client.container_groups.get(resource_group, cg_name)
        except Exception:
            return None

    @staticmethod
    def delete_session_container(interview_id: str):
        if not CodeContainerSessionService.is_azure_aci_configured():
            return

        cg_name = f"code-runner-{interview_id}"
        resource_group = settings.AZURE_ACI_RESOURCE_GROUP
        try:
            client = CodeContainerSessionService._get_client()
            logger.info(f"Deleting session container {cg_name}")
            client.container_groups.begin_delete(resource_group, cg_name)
        except Exception as e:
            logger.warning(f"Error deleting session container: {e}")

    @staticmethod
    def cleanup_old_containers():
        """Safeguard: Deletes containers older than 45 minutes"""
        if not CodeContainerSessionService.is_azure_aci_configured():
            return

        try:
            client = CodeContainerSessionService._get_client()
            resource_group = settings.AZURE_ACI_RESOURCE_GROUP
            cgs = client.container_groups.list_by_resource_group(resource_group)
            
            import datetime
            now = datetime.datetime.now(datetime.timezone.utc).timestamp()
            
            for cg in cgs:
                if cg.name and cg.name.startswith("code-runner-"):
                    if cg.tags and "creationTime" in cg.tags:
                        try:
                            creation_ts = float(cg.tags["creationTime"])
                            # 45 minutes = 2700 seconds
                            if now - creation_ts > 2700:
                                logger.info(f"Safeguard deleting old container: {cg.name}")
                                client.container_groups.begin_delete(resource_group, cg.name)
                        except Exception:
                            pass
        except Exception as e:
            logger.error(f"Error in cleanup: {e}")
