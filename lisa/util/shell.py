import logging
import os
import shutil
import sys
from logging import getLogger
from pathlib import Path, PurePath
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union, cast

import paramiko  # type: ignore
import spur  # type: ignore
import spurplus  # type: ignore
from retry import retry  # type: ignore

from lisa.util import InitializableMixin, LisaException


class ConnectionInfo:
    def __init__(
        self,
        address: str = "",
        port: int = 22,
        username: str = "root",
        password: Optional[str] = "",
        private_key_file: Optional[str] = None,
    ) -> None:
        self.address = address
        self.port = port
        self.username = username
        self.password = password
        self.private_key_file = private_key_file

        if not self.password and not self.private_key_file:
            raise LisaException(
                "at least one of password and privateKeyFile need to be set"
            )
        elif not self.private_key_file:
            # use password
            # spurplus doesn't process empty string correctly, use None
            self.private_key_file = None
        else:
            if not Path(self.private_key_file).exists():
                raise FileNotFoundError(self.private_key_file)
            self.password = None

        if not self.username:
            raise LisaException("username must be set")


class WindowsShellType(object):
    """
    Windows command generator
    Support get pid, set envs, and cwd
    Doesn't support kill, it needs overwrite spur.SshShell
    """

    supports_which = False

    def generate_run_command(
        self,
        command_args: List[str],
        store_pid: bool = False,
        cwd: Optional[str] = None,
        update_env: Optional[Dict[str, str]] = None,
        new_process_group: bool = False,
    ) -> str:
        commands = []

        if store_pid:
            commands.append(
                'powershell "(gwmi win32_process|? processid -eq $pid).parentprocessid"'
                " &&"
            )

        if cwd is not None:
            commands.append(f"cd {cwd} 2>&1 && echo spur-cd: 0 ")
            commands.append("|| echo spur-cd: 1 && exit 1 &")

        if update_env:
            update_env_commands = [
                "set {0}={1}".format(key, value) for key, value in update_env.items()
            ]
            commands += f"{'& '.join(update_env_commands)}& "

        if cwd is not None:
            commands.append(f"pushd {cwd} & ")
            commands.append(" ".join(command_args))
            commands.append(" & popd")
        else:
            commands.append(" ".join(command_args))
        return " ".join(commands)


# retry strategy is the same as spurplus.connect_with_retries.
@retry(Exception, tries=60, delay=1, logger=None)  # type: ignore
def try_connect(connection_info: ConnectionInfo) -> Any:
    # spur always run a linux command and will fail on Windows.
    # So try with paramiko firstly.
    paramiko_client = paramiko.SSHClient()
    paramiko_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    paramiko_client.connect(
        hostname=connection_info.address,
        port=connection_info.port,
        username=connection_info.username,
        password=connection_info.password,
        key_filename=connection_info.private_key_file,
    )
    _, stdout, _ = paramiko_client.exec_command("cmd")
    paramiko_client.close()

    return stdout


class SshShell(InitializableMixin):
    def __init__(self, connection_info: ConnectionInfo) -> None:
        super().__init__()
        self.is_remote = True
        self._connection_info = connection_info
        self._inner_shell: Optional[spur.SshShell] = None

        paramiko_logger = getLogger("paramiko")
        paramiko_logger.setLevel(logging.WARN)

    def _initialize(self, *args: Any, **kwargs: Any) -> None:
        try:
            stdout = try_connect(self._connection_info)
        except Exception as identifier:
            raise LisaException(
                f"connect to server "
                f"[{self._connection_info.address}:{self._connection_info.port}]"
                f" failed: {identifier}"
            )

        # Some windows doesn't end the text stream, so read first line only.
        # it's  enough to detect os.
        stdout_content = stdout.readline()
        stdout.close()

        if stdout_content and "Windows" in stdout_content:
            self.is_linux = False
            shell_type = WindowsShellType()
        else:
            self.is_linux = True
            shell_type = spur.ssh.ShellTypes.sh

        spur_kwargs = {
            "hostname": self._connection_info.address,
            "port": self._connection_info.port,
            "username": self._connection_info.username,
            "password": self._connection_info.password,
            "private_key_file": self._connection_info.private_key_file,
            "missing_host_key": spur.ssh.MissingHostKey.accept,
            "connect_timeout": 10,
        }

        spur_ssh_shell = spur.SshShell(shell_type=shell_type, **spur_kwargs)
        sftp = spurplus.sftp.ReconnectingSFTP(
            sftp_opener=spur_ssh_shell._open_sftp_client
        )
        self._inner_shell = spurplus.SshShell(spur_ssh_shell=spur_ssh_shell, sftp=sftp)

    def close(self) -> None:
        if self._inner_shell:
            self._inner_shell.close()
            # after closed, can be reconnect
            self._inner_shell = None
            self._is_initialized = False

    def spawn(
        self,
        command: Sequence[str],
        update_env: Optional[Mapping[str, str]] = None,
        store_pid: bool = False,
        cwd: Optional[Union[str, Path]] = None,
        stdout: Any = None,
        stderr: Any = None,
        encoding: str = "utf-8",
        use_pty: bool = False,
        allow_error: bool = True,
    ) -> Any:
        self.initialize()
        assert self._inner_shell
        return self._inner_shell.spawn(
            command=command,
            update_env=update_env,
            store_pid=store_pid,
            cwd=cwd,
            stdout=stdout,
            stderr=stderr,
            encoding=encoding,
            use_pty=use_pty,
            allow_error=allow_error,
        )

    def mkdir(
        self,
        path: PurePath,
        mode: int = 0o777,
        parents: bool = True,
        exist_ok: bool = False,
    ) -> None:
        path_str = self._purepath_to_str(path)
        self.initialize()
        assert self._inner_shell
        self._inner_shell.mkdir(path_str, mode=mode, parents=parents, exist_ok=exist_ok)

    def exists(self, path: PurePath) -> bool:
        self.initialize()
        assert self._inner_shell
        path_str = self._purepath_to_str(path)
        return cast(bool, self._inner_shell.exists(path_str))

    def remove(self, path: PurePath, recursive: bool = False) -> None:
        self.initialize()
        assert self._inner_shell
        path_str = self._purepath_to_str(path)
        self._inner_shell.remove(path_str, recursive)

    def chmod(self, path: PurePath, mode: int) -> None:
        self.initialize()
        assert self._inner_shell
        path_str = self._purepath_to_str(path)
        self._inner_shell.chmod(path_str, mode)

    def stat(self, path: PurePath) -> os.stat_result:
        self.initialize()
        assert self._inner_shell
        path_str = self._purepath_to_str(path)
        sftp_attributes: paramiko.SFTPAttributes = self._inner_shell.stat(path_str)

        result = os.stat_result(sftp_attributes.st_mode)
        result.st_mode = sftp_attributes.st_mode
        result.st_size = sftp_attributes.st_size
        result.st_uid = sftp_attributes.st_uid
        result.st_gid = sftp_attributes.st_gid
        result.st_atime = sftp_attributes.st_atime
        result.st_mtime = sftp_attributes.st_mtime
        return result

    def is_dir(self, path: PurePath) -> bool:
        self.initialize()
        assert self._inner_shell
        path_str = self._purepath_to_str(path)
        return cast(bool, self._inner_shell.is_dir(path_str))

    def is_symlink(self, path: PurePath) -> bool:
        self.initialize()
        assert self._inner_shell
        path_str = self._purepath_to_str(path)
        return cast(bool, self._inner_shell.is_symlink(path_str))

    def symlink(self, source: PurePath, destination: PurePath) -> None:
        self.initialize()
        assert self._inner_shell
        source_str = self._purepath_to_str(source)
        destination_str = self._purepath_to_str(destination)
        self._inner_shell.symlink(source_str, destination_str)

    def chown(self, path: PurePath, uid: int, gid: int) -> None:
        self.initialize()
        assert self._inner_shell
        path_str = self._purepath_to_str(path)
        self._inner_shell.chown(path_str, uid, gid)

    def copy(self, local_path: PurePath, node_path: PurePath) -> None:
        self.mkdir(node_path.parent, parents=True, exist_ok=True)
        self.initialize()
        assert self._inner_shell
        local_path_str = self._purepath_to_str(local_path)
        node_path_str = self._purepath_to_str(node_path)
        self._inner_shell.put(local_path_str, node_path_str, create_directories=True)

    def _purepath_to_str(
        self, path: Union[Path, PurePath, str]
    ) -> Union[Path, PurePath, str]:
        """
        spurplus doesn't support pure path, so it needs to convert.
        """
        if isinstance(path, PurePath):
            path = str(path)
        return path


class LocalShell(InitializableMixin):
    def __init__(self) -> None:
        super().__init__()
        self.is_remote = False
        self._inner_shell = spur.LocalShell()

    def _initialize(self, *args: Any, **kwargs: Any) -> None:
        if "win32" == sys.platform:
            self.is_linux = False
        else:
            self.is_linux = True

    def close(self) -> None:
        pass

    def spawn(
        self,
        command: Sequence[str],
        update_env: Optional[Mapping[str, str]] = None,
        store_pid: bool = False,
        cwd: Optional[Union[str, Path]] = None,
        stdout: Any = None,
        stderr: Any = None,
        encoding: str = "utf-8",
        use_pty: bool = False,
        allow_error: bool = False,
    ) -> Any:
        return self._inner_shell.spawn(
            command=command,
            update_env=update_env,
            store_pid=store_pid,
            cwd=cwd,
            stdout=stdout,
            stderr=stderr,
            encoding=encoding,
            use_pty=use_pty,
            allow_error=allow_error,
        )

    def mkdir(
        self,
        path: PurePath,
        mode: int = 0o777,
        parents: bool = True,
        exist_ok: bool = False,
    ) -> None:
        assert isinstance(path, Path), f"actual: {type(path)}"
        path.mkdir(mode=mode, parents=parents, exist_ok=exist_ok)

    def exists(self, path: PurePath) -> bool:
        assert isinstance(path, Path), f"actual: {type(path)}"
        return path.exists()

    def remove(self, path: PurePath, recursive: bool = False) -> None:
        assert isinstance(path, Path), f"actual: {type(path)}"
        path.rmdir()

    def chmod(self, path: PurePath, mode: int) -> None:
        assert isinstance(path, Path), f"actual: {type(path)}"
        path.chmod(mode)

    def stat(self, path: PurePath) -> os.stat_result:
        assert isinstance(path, Path), f"actual: {type(path)}"
        return path.stat()

    def is_dir(self, path: PurePath) -> bool:
        assert isinstance(path, Path), f"actual: {type(path)}"
        return path.is_dir()

    def is_symlink(self, path: PurePath) -> bool:
        assert isinstance(path, Path), f"actual: {type(path)}"
        return path.is_symlink()

    def symlink(self, source: PurePath, destination: PurePath) -> None:
        assert isinstance(source, Path), f"actual: {type(source)}"
        assert isinstance(destination, Path), f"actual: {type(destination)}"
        source.symlink_to(destination)

    def chown(self, path: PurePath, uid: int, gid: int) -> None:
        assert isinstance(path, Path), f"actual: {type(path)}"
        shutil.chown(path, cast(str, uid), cast(str, gid))

    def copy(self, local_path: PurePath, node_path: PurePath) -> None:
        self.mkdir(node_path.parent, parents=True, exist_ok=True)
        assert isinstance(local_path, Path), f"actual: {type(local_path)}"
        assert isinstance(node_path, Path), f"actual: {type(node_path)}"
        shutil.copy(local_path, node_path)


Shell = Union[LocalShell, SshShell]
