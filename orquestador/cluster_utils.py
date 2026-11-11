"""Utilidades para consultar los workers por SSH (se ejecutan desde server4)."""
from __future__ import annotations

import subprocess

import config_cluster as cfg


def ssh(worker: str, comando: str, timeout: int = 30) -> tuple[int, str]:
    """Ejecuta un comando en un worker. Devuelve (código, salida)."""
    ip = cfg.WORKERS[worker]["ip"]
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             f"{cfg.USUARIO_SSH}@{ip}", comando],
            capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 255, f"no se pudo ejecutar ssh: {exc}"
    return r.returncode, (r.stdout + r.stderr).strip()


def nombres_vms() -> list[str]:
    return [f"{cfg.NOMBRE_SLICE}-vm{i}" for i in range(1, 7)]


def worker_de(vm: str) -> str:
    """Round robin de la EX1: vm1,vm4 -> worker1; vm2,vm5 -> worker2; vm3,vm6 -> worker3."""
    n = int(vm.rsplit("vm", 1)[-1])
    return f"worker{(n - 1) % 3 + 1}"
