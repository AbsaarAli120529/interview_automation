"""
Code Execution Service
----------------------
Runs candidate source code inside isolated Docker containers (Local) or Azure Container Instances (ACI).

Security model (Local):
  - Network disabled (--network none)
  - Memory capped at 256 MB
  - CPU capped at 1 core
  - PID limit of 50
  - Container removed after execution (--rm)
  - Hard timeout of 10 seconds per run
"""

import os
import subprocess
import tempfile
import logging
import base64
import uuid
import time
from typing import Optional
from app.core.config import settings

logger = logging.getLogger(__name__)

try:
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.containerinstance import ContainerInstanceManagementClient
    from azure.mgmt.containerinstance.models import (
        ContainerGroup,
        Container,
        ContainerPort,
        EnvironmentVariable,
        ResourceRequests,
        ResourceRequirements,
        OperatingSystemTypes,
        ImageRegistryCredential,
    )
    AZURE_SDK_AVAILABLE = True
except ImportError:
    AZURE_SDK_AVAILABLE = False

# ---------------------------------------------------------------------------
# Security limits
# ---------------------------------------------------------------------------
TIMEOUT_SECONDS = 10
MEMORY_LIMIT = "256m"

# ---------------------------------------------------------------------------
# Per-language configuration
# ---------------------------------------------------------------------------
# Each entry defines:
#   image       – Docker image to use (must be pre-built on the host for local, or ACR for Azure)
#   filename    – name given to the source file inside /code/
#   compile_cmd – shell tokens run before the program (None = interpreted)
#   run_cmd     – shell tokens used to execute the program
# ---------------------------------------------------------------------------
LANGUAGE_CONFIG: dict[str, dict] = {
    "python3": {
        "image": "code-runner-python",
        "filename": "solution.py",
        "compile_cmd": None,
        "run_cmd": ["python3", "/code/solution.py"],
    },
    "javascript": {
        "image": "code-runner-javascript",
        "filename": "solution.js",
        "compile_cmd": None,
        "run_cmd": ["node", "/code/solution.js"],
    },
    "java": {
        "image": "code-runner-java",
        "filename": "Solution.java",
        "compile_cmd": ["javac", "/code/Solution.java"],
        "run_cmd": ["java", "-cp", "/code", "Solution"],
    },
    "cpp": {
        "image": "code-runner-cpp",
        "filename": "solution.cpp",
        "compile_cmd": ["g++", "-O2", "-o", "/code/solution", "/code/solution.cpp"],
        "run_cmd": ["/code/solution"],
    },
}

# ---------------------------------------------------------------------------
# Base Docker flags applied to every container run (LOCAL ONLY)
# ---------------------------------------------------------------------------
_DOCKER_BASE_FLAGS = [
    "--rm",
    "-i",
    "--network", "none",
    f"--memory={MEMORY_LIMIT}",
    "--cpus=1",
    "--pids-limit=50",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_subprocess(
    cmd: list[str],
    stdin_data: Optional[str] = None,
    timeout: int = TIMEOUT_SECONDS,
) -> dict:
    """Execute *cmd* as a subprocess and return a normalised result dict."""
    try:
        result = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "timed_out": False,
            "error": None,
        }
    except subprocess.TimeoutExpired:
        logger.warning("Subprocess timed out: %s", cmd)
        return {
            "stdout": "",
            "stderr": "",
            "exit_code": -1,
            "timed_out": True,
            "error": f"Execution timed out after {timeout} seconds.",
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error running subprocess: %s", exc)
        return {
            "stdout": "",
            "stderr": "",
            "exit_code": -1,
            "timed_out": False,
            "error": str(exc),
        }


def _docker_run_cmd(image: str, mount_dir: str, run_cmd: list[str]) -> list[str]:
    """Build the full ``docker run`` command list."""
    return [
        "docker", "run",
        *_DOCKER_BASE_FLAGS,
        "-v", f"{mount_dir}:/code:ro",  # mount source dir as read-only
        image,
        *run_cmd,
    ]


def _docker_compile_cmd(image: str, mount_dir: str, compile_cmd: list[str]) -> list[str]:
    """Build a ``docker run`` command for compilation."""
    return [
        "docker", "run",
        *_DOCKER_BASE_FLAGS,
        "-v", f"{mount_dir}:/code",  # read-write for compiler output
        image,
        *compile_cmd,
    ]


def is_azure_aci_configured() -> bool:
    """Check if Azure Container Instances configuration is present."""
    return bool(getattr(settings, "AZURE_SUBSCRIPTION_ID", None) and getattr(settings, "AZURE_ACI_RESOURCE_GROUP", None))


# ---------------------------------------------------------------------------
# Azure Execution
# ---------------------------------------------------------------------------
def _execute_code_azure(
    config: dict,
    source_code: str,
    stdin_input: Optional[str] = None,
    interview_id: Optional[str] = None,
) -> dict:
    if not AZURE_SDK_AVAILABLE:
        return {
            "stdout": "", "stderr": "", "exit_code": -1, "timed_out": False,
            "error": "Azure AD / ACI SDKs not installed. Please install azure-identity and azure-mgmt-containerinstance."
        }

    sub_id = settings.AZURE_SUBSCRIPTION_ID
    resource_group = settings.AZURE_ACI_RESOURCE_GROUP
    location = getattr(settings, "AZURE_ACI_LOCATION", "eastus")
    acr_server = getattr(settings, "AZURE_ACR_SERVER", "")
    acr_user = getattr(settings, "AZURE_ACR_USERNAME", None)
    acr_pass = getattr(settings, "AZURE_ACR_PASSWORD", None)

    # For Azure, the image should be pulled from ACR (or a public registry if acr_server is empty)
    img = f"{acr_server}/{config['image']}:latest" if acr_server else f"{config['image']}:latest"

    # We use a trick to encode the code/input and decode it inside the container using shell
    src_b64 = base64.b64encode(source_code.encode("utf-8")).decode("utf-8")
    std_b64 = base64.b64encode((stdin_input or "").encode("utf-8")).decode("utf-8")
    
    filename = config["filename"]
    
    # Constructing a single bash command to write code, compile (if needed), and run logic
    cmds = [
        "mkdir -p /code",
        f"printf '%s' '{src_b64}' | base64 -d > /code/{filename}"
    ]
    if config["compile_cmd"]:
        comp_str = " ".join(config["compile_cmd"])
        # Append logic to only run if compilation succeeds
        cmds.append(f"{comp_str}")
        
    run_str = " ".join(config["run_cmd"])
    # 5 second timeout required
    cmds.append(f"printf '%s' '{std_b64}' | base64 -d | timeout 5 {run_str}")
    
    full_cmd = " && ".join(cmds)
    
    wrapped_cmd = f"{{ {full_cmd} ; }} ; echo \"__EXIT_CODE__$?\""
    b64_cmd = base64.b64encode(wrapped_cmd.encode("utf-8")).decode("utf-8")
    final_entrypoint = f"/bin/sh -c 'printf \"%s\" \"{b64_cmd}\" | base64 -d | sh'"
    
    if interview_id:
        cg_name = f"code-runner-{interview_id}"
        container_name = "runner"
        try:
            credential = DefaultAzureCredential()
            client = ContainerInstanceManagementClient(credential, sub_id)
            
            cg = client.container_groups.get(resource_group, cg_name)
            if cg.instance_view and cg.instance_view.state != "Running":
                return {
                    "stdout": "", "stderr": "", "exit_code": -1, "timed_out": False,
                    "error": f"Container is not running. State: {cg.instance_view.state}"
                }
            
            from azure.mgmt.containerinstance.models import ContainerExecRequest, ContainerExecRequestTerminalSize
            import websockets.sync.client
            from websockets.exceptions import ConnectionClosed
            import re
            
            exec_req = ContainerExecRequest(
                command=final_entrypoint,
                terminal_size=ContainerExecRequestTerminalSize(rows=24, cols=80)
            )
            
            resp = client.containers.execute_command(
                resource_group,
                cg_name,
                container_name,
                exec_req
            )
            
            logs = ""
            try:
                with websockets.sync.client.connect(resp.web_socket_uri) as ws:
                    ws.send(resp.password)
                    while True:
                        try:
                            msg = ws.recv(timeout=10)
                            if isinstance(msg, bytes):
                                msg = msg.decode("utf-8")
                            logs += msg
                        except TimeoutError:
                            continue
                        except ConnectionClosed:
                            break
                        except Exception:
                            break
            except Exception as wse:
                logger.error(f"Websocket error: {wse}")
            
            exit_code = -1
            timed_out = False
            
            match = re.search(r"__EXIT_CODE__(\d+)", logs)
            if match:
                exit_code = int(match.group(1))
                logs = logs[:match.start()].strip()
            else:
                if len(logs) == 0 or "Terminated" in logs:
                    timed_out = True
                
            return {
                "stdout": logs if exit_code == 0 else "",
                "stderr": logs if exit_code != 0 else "",
                "exit_code": exit_code,
                "timed_out": timed_out,
                "error": "Timeout or error in ACI" if exit_code != 0 and not logs else None,
            }
        except Exception as e:
            logger.exception("Azure Exec on Running Container Error")
            return {
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
                "timed_out": False,
                "error": str(e),
            }
    else:
        # Fallback to creating a new container
        entrypoint = ["/bin/sh", "-c", wrapped_cmd]
        cg_name = f"code-run-{uuid.uuid4().hex[:8]}"
        container_name = "runner"
    
        try:
            credential = DefaultAzureCredential()
            client = ContainerInstanceManagementClient(credential, sub_id)
    
            container_resource = Container(
                name=container_name,
                image=img,
                resources=ResourceRequirements(requests=ResourceRequests(memory_in_gb=0.5, cpu=1.0)),
                command=entrypoint,
            )
    
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
                restart_policy="Never", # single execution
                image_registry_credentials=registry_credentials if registry_credentials else None
            )
    
            # Deploy container
            logger.info(f"Creating Azure Container Instance: {cg_name}")
            poller = client.container_groups.begin_create_or_update(resource_group, cg_name, group)
            poller.result() # Wait for deployment
    
            # Wait until terminal state
            terminal_states = ["Succeeded", "Failed", "Terminated"]
            max_waits = 30 # approx 1 minute max wait time on top of deployment
            logs = ""
            exit_code = 0
            timed_out = False
    
            for _ in range(max_waits):
                cg = client.container_groups.get(resource_group, cg_name)
                state = cg.instance_view.state if cg.instance_view else "Unknown"
                c_state = cg.containers[0].instance_view.current_state.state if cg.containers[0].instance_view and cg.containers[0].instance_view.current_state else "Unknown"
                
                if c_state in terminal_states:
                    try:
                        c_props = cg.containers[0].instance_view.current_state
                        exit_code = c_props.exit_code if c_props.exit_code is not None else 0
                    except AttributeError:
                        pass
                    break
                time.sleep(2)
            else:
                timed_out = True
    
            # Fetch logs (stdout and stderr combined in ACI by default)
            try:
                log_result = client.containers.list_logs(resource_group, cg_name, container_name)
                logs = log_result.content
            except Exception:
                logs = "Failed to fetch logs."
    
            # Cleanup
            client.container_groups.begin_delete(resource_group, cg_name)
            
            import re
            match = re.search(r"__EXIT_CODE__(\d+)", logs)
            if match:
                exit_code = int(match.group(1))
                logs = logs[:match.start()].strip()
    
            return {
                "stdout": logs if exit_code == 0 else "",
                "stderr": logs if exit_code != 0 else "",
                "exit_code": exit_code,
                "timed_out": timed_out,
                "error": "Timeout in ACI" if timed_out else None,
            }
    
        except Exception as e:
            logger.exception("Azure Execution Error")
        return {
            "stdout": "",
            "stderr": "",
            "exit_code": -1,
            "timed_out": False,
            "error": str(e),
        }

# ---------------------------------------------------------------------------
# Local Execution
# ---------------------------------------------------------------------------
def _execute_code_local(
    config: dict,
    source_code: str,
    stdin_input: Optional[str] = None,
) -> dict:
    image = config["image"]
    filename = config["filename"]
    compile_cmd = config["compile_cmd"]
    run_cmd = config["run_cmd"]

    with tempfile.TemporaryDirectory() as tmp_dir:
        source_path = os.path.join(tmp_dir, filename)
        try:
            with open(source_path, "w", encoding="utf-8") as fh:
                fh.write(source_code)
        except OSError as exc:
            return {
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
                "timed_out": False,
                "error": f"Failed to write source file: {exc}",
            }

        if compile_cmd:
            compile_docker_cmd = _docker_compile_cmd(image, tmp_dir, compile_cmd)
            compile_result = _run_subprocess(compile_docker_cmd, timeout=TIMEOUT_SECONDS)

            if compile_result["timed_out"] or compile_result["exit_code"] != 0:
                return {
                    "stdout": compile_result["stdout"],
                    "stderr": compile_result["stderr"] or compile_result["error"],
                    "exit_code": compile_result["exit_code"],
                    "timed_out": compile_result["timed_out"],
                    "error": "Compilation failed.",
                }

        run_docker_cmd = _docker_run_cmd(image, tmp_dir, run_cmd)
        return _run_subprocess(run_docker_cmd, stdin_data=stdin_input, timeout=TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def execute_code(
    language: str,
    source_code: str,
    stdin_input: Optional[str] = None,
    interview_id: Optional[str] = None,
) -> dict:
    lang = language.lower().strip()
    if lang not in LANGUAGE_CONFIG:
        supported = ", ".join(LANGUAGE_CONFIG.keys())
        return {
            "stdout": "",
            "stderr": "",
            "exit_code": -1,
            "timed_out": False,
            "error": f"Unsupported language '{language}'. Supported: {supported}",
        }

    config = LANGUAGE_CONFIG[lang]

    # Check if Azure Container Instances applies
    if is_azure_aci_configured():
        return _execute_code_azure(config, source_code, stdin_input, interview_id=interview_id)
    else:
        return _execute_code_local(config, source_code, stdin_input)


def run_test_cases(
    language: str,
    source_code: str,
    test_cases: list[dict],
    interview_id: Optional[str] = None,
) -> list[dict]:
    results = []

    for tc in test_cases:
        tc_id = tc.get("id")
        stdin_input: str = tc.get("input", "")
        expected_output: str = tc.get("expected_output", "")

        exec_result = execute_code(
            language=language,
            source_code=source_code,
            stdin_input=stdin_input,
            interview_id=interview_id,
        )

        actual_output: str = exec_result["stdout"]
        actual_stripped = actual_output.strip()
        expected_stripped = expected_output.strip()

        execution_error = exec_result.get("error")
        timed_out = exec_result.get("timed_out", False)

        if timed_out:
            passed = False
            error_msg = exec_result["error"]
        elif execution_error:
            passed = False
            error_msg = execution_error
        elif exec_result["exit_code"] != 0:
            passed = False
            error_msg = exec_result["stderr"] or f"Non-zero exit code: {exec_result['exit_code']}"
        else:
            passed = actual_stripped == expected_stripped
            error_msg = None

        results.append({
            "test_case_id": tc_id,
            "input": stdin_input,
            "expected_output": expected_output,
            "actual_output": actual_output,
            "passed": passed,
            "error": error_msg,
        })

    return results

