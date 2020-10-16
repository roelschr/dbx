import asyncio
import logging
import os
import pathlib
from typing import Tuple, Optional
from dbx.cli.execute import execute_command
from cryptography.hazmat.backends import default_backend as crypto_default_backend
from cryptography.hazmat.primitives import serialization as crypto_serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dbx.utils.common import ApiV1Client, TunnelInfo, get_ssh_client
from dbx.utils.watchdog.context_manager import ContextManager
from sshconf import read_ssh_config


class SSHConfig:

    def __init__(self, conf_file: str = str(pathlib.Path("~/.ssh/config").expanduser())):
        self._conf = read_ssh_config(conf_file)

    def get(self, cluster_id: str) -> Optional[TunnelInfo]:
        _host = self._conf.host(cluster_id)
        if _host:
            return TunnelInfo(host=_host["hostname"], port=int(_host["port"]), private_key_file=_host["identityfile"])

    def set(self, cluster_id: str, info: TunnelInfo):
        if cluster_id in self._conf.hosts():
            self._conf.set(cluster_id,
                           Hostname=info.host,
                           Port=str(info.port),
                           User='root',
                           IdentityFile=info.private_key_file)
        else:
            self._conf.add(cluster_id,
                           Hostname=info.host,
                           Port=str(info.port),
                           User='root',
                           IdentityFile=info.private_key_file)
        self._conf.save()

    def remove(self, cluster_id: str):
        self._conf.remove(cluster_id)
        self._conf.save()


class TunnelManager:
    COMMANDS = {
        'install_libraries': '%pip install pyngrok pathlib',
        'stop_ngrok': """
            import os
            os.system('pkill -f ngrok')
        """,
        'install_ssh_keys': """
        from pathlib import Path
        private_key = b\"\"\"{private_key}\"\"\"
        public_key = b\"\"\"{public_key}\"\"\"
        Path("~/.ssh").expanduser().mkdir(exist_ok=True)
        Path("~/.ssh/id_rsa").expanduser().write_bytes(private_key)
        Path("~/.ssh/id_rsa.pub").expanduser().write_bytes(public_key)
        Path("~/.ssh/authorized_keys").expanduser().write_bytes(public_key)
        """,
        'generate_url': """
            from pyngrok import ngrok
            ngrok.set_auth_token('{token}')
            ssh_url = ngrok.connect(22, "tcp")
        """,
        'print_ssh_url': 'print(ssh_url)'
    }

    def __init__(self, api_v1_client: ApiV1Client, cluster_id: str, context_manager: ContextManager):
        self._api_v1_client = api_v1_client
        self._context_manager = context_manager
        self._status = "initializing"
        self._cluster_id = cluster_id
        self._ssh_conf = SSHConfig()
        self.tunnel_info = self._ssh_conf.get(self._cluster_id)

    def _exec(self, cmd, verbose=False):
        return execute_command(self._api_v1_client, self._cluster_id, self._context_manager.context_id, cmd, verbose)

    @property
    def status(self):
        return self._status

    @staticmethod
    def _parse_url(url: str) -> Tuple[str, int]:
        (host, port) = url.replace('tcp://', "").split(":")
        return host, int(port)

    def _prepare_sshd(self):
        self._status = "preparing sshd service"
        logging.info(self._status)
        client = get_ssh_client(self.tunnel_info)
        client.exec_command("mkdir -p /usr/lib/ssh")
        client.exec_command("ln -s /usr/lib/openssh/sftp-server /usr/lib/ssh/sftp-server")
        client.exec_command("systemctl restart ssh.service")
        self._status = "preparing sshd service - done"
        logging.info(self._status)

    async def tunnel_routine(self):
        while True:
            if not self._context_manager.status == "running":
                self._status = "waiting for the context"
                await asyncio.sleep(5)
            else:
                if self.tunnel_info:
                    self._status = "checking cached tunnel url"
                    try:
                        await self._check_tunnel()
                        self._status = "running"
                        await asyncio.sleep(5)
                    except KeyboardInterrupt:
                        logging.error("Gracefully stopping tunnel manager")
                    except Exception as e:
                        logging.error(f"Error on tunnel check: {e}")
                        self._status = "tunnel is unreachable, initializing a new one"
                        try:
                            await self.initialize_tunnel()
                        except Exception as e:
                            logging.info(f"Error on tunnel initialization: {e}")
                            self.tunnel_info = None
                            self._ssh_conf.remove(self._cluster_id)
                            await asyncio.sleep(2)
                    await asyncio.sleep(5)
                else:
                    self._status = "initializing a plain new tunnel"
                    await self.initialize_tunnel()
                    await asyncio.sleep(5)

    async def initialize_tunnel(self):
        self._status = "installing libraries"
        logging.info(self._status)
        self._exec(self.COMMANDS["install_libraries"])
        self._status = "restarting tunnel appliance"
        logging.info(self._status)
        self._exec(self.COMMANDS["stop_ngrok"])
        self._status = "preparing ssh keys"
        logging.info(self._status)

        private_key, public_key = await self.generate_key_pair()

        private_key_path = pathlib.Path("~/.ssh/%s" % self._cluster_id).expanduser()

        if private_key_path.exists():
            private_key_path.unlink()

        private_key_path.write_bytes(private_key)
        os.chmod(private_key_path, 0o600)

        remote_keys_cmd = self.COMMANDS['install_ssh_keys'].format(
            private_key=private_key.decode('utf-8'),
            public_key=public_key.decode('utf-8')
        )

        self._exec(remote_keys_cmd, verbose=False)
        self._status = "generating tunnel url"
        logging.info(self._status)

        self._exec(self.COMMANDS['generate_url'].format(token=os.environ["DBX_NGROK_TOKEN"]))
        self._status = "generating tunnel url - done"
        logging.info(self._status)

        raw_ssh_url = self._exec(self.COMMANDS['print_ssh_url'])
        logging.info(raw_ssh_url)
        _host, _port = self._parse_url(raw_ssh_url)
        self.tunnel_info = TunnelInfo(_host, _port, str(private_key_path))
        logging.info(f"Tunnel info object: {self.tunnel_info}")
        try:
            self._ssh_conf.set(self._cluster_id, self.tunnel_info)
        except Exception as e:
            logging.info(f"Error on config safe: {e}")
        logging.info("Local ssh config prepared")
        self._prepare_sshd()

    async def _check_tunnel(self):
        client = get_ssh_client(self.tunnel_info)
        client.exec_command("ls -la")

    @staticmethod
    async def generate_key_pair() -> Tuple[bytes, bytes]:
        key = rsa.generate_private_key(
            backend=crypto_default_backend(),
            public_exponent=65537,
            key_size=2048
        )

        private_key = key.private_bytes(
            crypto_serialization.Encoding.PEM,
            crypto_serialization.PrivateFormat.TraditionalOpenSSL,
            crypto_serialization.NoEncryption()
        )

        public_key = key.public_key().public_bytes(
            crypto_serialization.Encoding.OpenSSH,
            crypto_serialization.PublicFormat.OpenSSH
        )
        return private_key, public_key
