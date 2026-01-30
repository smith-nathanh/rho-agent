"""MySQL container management for DBBench evaluation."""

import asyncio
import uuid


class MySQLContainer:
    """Manages a MySQL 8 Docker container for DBBench evaluation.

    Provides methods to start, create/drop databases, and cleanup.
    Uses tmpfs for fast ephemeral storage and tuned settings for eval workloads.
    """

    IMAGE = "mysql:8"
    PASSWORD = "evalpass"
    PORT = 3306

    def __init__(self) -> None:
        """Initialize the MySQL container manager."""
        self._container_id: str | None = None
        self._name: str | None = None
        self._host: str | None = None

    @property
    def is_running(self) -> bool:
        """Check if the container is running."""
        return self._container_id is not None

    @property
    def container_id(self) -> str | None:
        """Get the container ID."""
        return self._container_id

    async def start(self) -> None:
        """Start the MySQL container and wait for it to be healthy."""
        if self._container_id:
            return  # Already running

        self._name = f"rho-eval-mysql-{uuid.uuid4().hex[:8]}"

        # Start MySQL with performance-tuned settings
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            self._name,
            "-e",
            f"MYSQL_ROOT_PASSWORD={self.PASSWORD}",
            "--tmpfs",
            "/var/lib/mysql:rw,uid=999,gid=999",
            self.IMAGE,
            "--max_connections=200",
            "--innodb_flush_log_at_trx_commit=0",
            "--skip-name-resolve",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"Failed to start MySQL container: {error}")

        self._container_id = stdout.decode("utf-8").strip()

        # Get container IP
        self._host = await self._get_container_ip()

        # Wait for MySQL to be ready
        await self._wait_healthy()

    async def _get_container_ip(self) -> str:
        """Get the container's IP address."""
        cmd = [
            "docker",
            "inspect",
            "-f",
            "{{.NetworkSettings.IPAddress}}",
            self._container_id,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, _ = await proc.communicate()
        return stdout.decode("utf-8").strip()

    async def _wait_healthy(self, timeout: int = 60) -> None:
        """Wait for MySQL to accept connections and be fully initialized."""
        start = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start < timeout:
            # Test that we can actually execute a query with auth
            cmd = [
                "docker",
                "exec",
                self._container_id,
                "mysql",
                "-u",
                "root",
                f"-p{self.PASSWORD}",
                "-e",
                "SELECT 1",
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                return

            await asyncio.sleep(1)

        raise TimeoutError(f"MySQL not ready after {timeout} seconds")

    def get_connection_info(self) -> dict[str, str | int]:
        """Get connection parameters for mysql-connector-python.

        Returns:
            Dict with host, port, user, password keys
        """
        if not self._host:
            raise RuntimeError("Container not started")

        return {
            "host": self._host,
            "port": self.PORT,
            "user": "root",
            "password": self.PASSWORD,
        }

    async def create_database(self, name: str) -> None:
        """Create a new database."""
        if not self._container_id:
            raise RuntimeError("Container not started")

        cmd = [
            "docker",
            "exec",
            self._container_id,
            "mysql",
            "-u",
            "root",
            f"-p{self.PASSWORD}",
            "-e",
            f"CREATE DATABASE `{name}`;",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"Failed to create database: {error}")

    async def drop_database(self, name: str) -> None:
        """Drop a database if it exists."""
        if not self._container_id:
            return

        cmd = [
            "docker",
            "exec",
            self._container_id,
            "mysql",
            "-u",
            "root",
            f"-p{self.PASSWORD}",
            "-e",
            f"DROP DATABASE IF EXISTS `{name}`;",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    async def cleanup(self) -> None:
        """Stop and remove the container."""
        if not self._container_id:
            return

        cmd = ["docker", "rm", "-f", self._container_id]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        self._container_id = None
        self._name = None
        self._host = None

    async def __aenter__(self) -> "MySQLContainer":
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.cleanup()
