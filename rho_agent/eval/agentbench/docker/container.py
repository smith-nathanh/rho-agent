"""Docker container management for OS interaction evaluation."""

import asyncio
import uuid
from typing import Any


# Docker image mapping
IMAGE_MAP = {
    "default": "local-os/default",
    "packages": "local-os/packages",
    "ubuntu": "local-os/ubuntu",
}


class EvalContainer:
    """Manages a Docker container for OS interaction evaluation.

    Provides methods to start, execute commands, and cleanup containers.
    Uses AgentBench's Docker images for compatibility.
    """

    def __init__(self, image: str = "default") -> None:
        """Initialize the container manager.

        Args:
            image: Image identifier ("default", "packages", "ubuntu") or full image name
        """
        # Map short names to full image names
        self._image = IMAGE_MAP.get(image, image)
        self._container_id: str | None = None
        self._name: str | None = None

    @property
    def is_running(self) -> bool:
        """Check if the container is running."""
        return self._container_id is not None

    @property
    def container_id(self) -> str | None:
        """Get the container ID."""
        return self._container_id

    async def start(self) -> None:
        """Start the Docker container."""
        if self._container_id:
            return  # Already running

        # Generate unique container name
        self._name = f"rho-eval-{uuid.uuid4().hex[:8]}"

        # Start container in detached mode with bash
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            self._name,
            "-it",  # Interactive with TTY
            "--rm",  # Auto-remove when stopped
            self._image,
            "/bin/bash",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"Failed to start container: {error}")

        self._container_id = stdout.decode("utf-8").strip()

    async def execute(
        self,
        command: str,
        timeout: int = 120,
        working_dir: str | None = None,
    ) -> tuple[int, str, str]:
        """Execute a command in the container.

        Args:
            command: Shell command to execute
            timeout: Timeout in seconds
            working_dir: Optional working directory

        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        if not self._container_id:
            raise RuntimeError("Container not started")

        # Build docker exec command
        exec_cmd = ["docker", "exec"]

        if working_dir:
            exec_cmd.extend(["-w", working_dir])

        exec_cmd.extend([self._container_id, "/bin/bash", "-c", command])

        proc = await asyncio.create_subprocess_exec(
            *exec_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # Kill the exec process
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"Command timed out after {timeout} seconds")

        exit_code = proc.returncode or 0
        stdout_str = stdout.decode("utf-8", errors="replace")
        stderr_str = stderr.decode("utf-8", errors="replace")

        return exit_code, stdout_str, stderr_str

    async def run_init(self, code: str) -> None:
        """Run initialization code in the container.

        Args:
            code: Shell code to execute for initialization
        """
        if not code:
            return

        exit_code, stdout, stderr = await self.execute(code, timeout=60)
        if exit_code != 0:
            raise RuntimeError(f"Init script failed: {stderr}")

    async def run_init_file(self, file_path: str) -> None:
        """Run an initialization script file in the container.

        Reads the script content from host and runs it in the container
        via docker exec (matching AgentBench's approach).

        Args:
            file_path: Path to script file on host
        """
        if not file_path:
            return

        # Read script content from host
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                script_content = f.read()
        except FileNotFoundError:
            raise RuntimeError(f"Init script file not found: {file_path}")

        # Run script content in container via docker exec
        await self.run_init(script_content)

    async def run_background(self, script: str) -> None:
        """Run a script as a background process.

        Args:
            script: Shell command to run in background
        """
        if not script:
            return

        # The script likely already has & at the end, but make sure
        # We use nohup to prevent it from being killed when the exec session ends
        bg_script = script
        if not bg_script.strip().endswith("&"):
            bg_script = bg_script + " &"

        # Use nohup and redirect output to prevent blocking
        exit_code, stdout, stderr = await self.execute(
            f"nohup {bg_script} > /dev/null 2>&1",
            timeout=10,
        )

        # Give the background process time to start
        await asyncio.sleep(1)

    async def cleanup(self) -> None:
        """Stop and remove the container."""
        if not self._container_id:
            return

        # Stop the container (--rm flag will auto-remove it)
        stop_cmd = ["docker", "stop", self._container_id]

        proc = await asyncio.create_subprocess_exec(
            *stop_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        self._container_id = None
        self._name = None

    async def __aenter__(self) -> "EvalContainer":
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.cleanup()
