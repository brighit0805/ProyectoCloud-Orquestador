"""Medición del USO REAL de los hosts y de sus VMs (HLD dominio D, mínimo).

``recursos`` y ``ResourceMonitor`` informan lo ASIGNADO (reservas de vCPU/RAM).
Este módulo lee lo realmente UTILIZADO, para ver el efecto de una carga
(``stress-ng``) y compararlo con lo asignado:

* por host: CPU % (``/proc/stat``) y RAM usada (``/proc/meminfo``);
* por VM: CPU % (``cpu.time`` de libvirt entre dos muestras) y RAM usada
  (``balloon.available - balloon.unused`` si el invitado la informa; si no,
  el RSS del proceso QEMU en el host).

Se conecta por SSH a cada host (Linux: server1..4; OpenStack: los workers, que
también son hosts libvirt) y corre UN solo script remoto por host, que toma
dos muestras separadas por ``intervalo_s``. No instala nada en los hosts.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable

SECCION = "##"


class ErrorMedicion(RuntimeError):
    """No se pudo medir un host (SSH, permisos o salida inesperada)."""


def script_remoto(intervalo_s: float) -> str:
    """Script bash que imprime dos muestras (con marca de tiempo) del host."""
    return f"""
V="virsh -c qemu:///system"
$V list >/dev/null 2>&1 || V="sudo -n virsh -c qemu:///system"
muestra() {{
  echo "{SECCION}T $(date +%s.%N)"
  head -1 /proc/stat
  echo "{SECCION}DOM"
  $V domstats --cpu-total --balloon --vcpu 2>/dev/null
  echo "{SECCION}FIN"
}}
echo "{SECCION}M1"; muestra
sleep {float(intervalo_s)}
echo "{SECCION}M2"; muestra
echo "{SECCION}MEM"
grep -E '^(MemTotal|MemAvailable):' /proc/meminfo
"""


# --------------------------------------------------------------------- parseo
def parsear_domstats(texto: str) -> dict[str, dict[str, float]]:
    """``virsh domstats`` -> {dominio: {campo: valor numérico}}."""
    out: dict[str, dict[str, float]] = {}
    actual: dict[str, float] | None = None
    for linea in texto.splitlines():
        m = re.match(r"^Domain: '(.+)'\s*$", linea)
        if m:
            actual = out.setdefault(m.group(1), {})
            continue
        if actual is not None and "=" in linea:
            k, _, v = linea.strip().partition("=")
            try:
                actual[k] = float(v)
            except ValueError:
                pass
    return out


def parsear_cpu_proc_stat(linea: str) -> tuple[float, float]:
    """Primera línea de /proc/stat -> (ocioso, total) en jiffies."""
    campos = [float(x) for x in linea.split()[1:9]]
    campos += [0.0] * (8 - len(campos))
    ocioso = campos[3] + campos[4]            # idle + iowait
    return ocioso, sum(campos)


def _seccion(texto: str, ini: str, fin: str | None) -> str:
    a = texto.index(f"{SECCION}{ini}")
    b = texto.index(f"{SECCION}{fin}", a) if fin else len(texto)
    return texto[a:b]


def _muestra(bloque: str) -> dict:
    t = float(re.search(rf"^{re.escape(SECCION)}T (\S+)", bloque, re.M).group(1))
    cpu_linea = next(l for l in bloque.splitlines() if l.startswith("cpu "))
    dom = bloque.split(f"{SECCION}DOM", 1)[1].split(f"{SECCION}FIN", 1)[0]
    return {"t": t, "cpu": parsear_cpu_proc_stat(cpu_linea), "dom": parsear_domstats(dom)}


@dataclass
class UsoVM:
    dominio: str
    vcpu: int
    cpu_pct: float            # % del total de sus vCPU
    ram_asignada_mb: float
    ram_usada_mb: float
    ram_origen: str           # 'invitado' (balloon) o 'rss' (proceso QEMU en el host)


@dataclass
class UsoHost:
    host: str
    cpu_pct: float
    ram_total_mb: float
    ram_usada_mb: float
    vms: list[UsoVM] = field(default_factory=list)


def interpretar(salida: str, host: str) -> UsoHost:
    """Convierte la salida del script remoto en un ``UsoHost``."""
    try:
        m1 = _muestra(_seccion(salida, "M1", "M2"))
        m2 = _muestra(_seccion(salida, "M2", "MEM"))
        mem = _seccion(salida, "MEM", None)
        total_kb = float(re.search(r"MemTotal:\s+(\d+)", mem).group(1))
        disp_kb = float(re.search(r"MemAvailable:\s+(\d+)", mem).group(1))
    except (ValueError, AttributeError, StopIteration):
        raise ErrorMedicion(f"Salida inesperada al medir {host}: {salida[:200]!r}") from None
    dt = max(m2["t"] - m1["t"], 1e-6)
    d_ocioso = m2["cpu"][0] - m1["cpu"][0]
    d_total = m2["cpu"][1] - m1["cpu"][1]
    cpu_host = 100.0 * (1.0 - d_ocioso / d_total) if d_total > 0 else 0.0
    uso = UsoHost(host=host, cpu_pct=round(cpu_host, 1),
                  ram_total_mb=round(total_kb / 1024, 1),
                  ram_usada_mb=round((total_kb - disp_kb) / 1024, 1))
    for nombre, b in m2["dom"].items():
        a = m1["dom"].get(nombre)
        if a is None or "cpu.time" not in a or "cpu.time" not in b:
            continue                                   # la VM no existía en la 1ª muestra
        vcpu = int(b.get("vcpu.current") or b.get("vcpu.maximum") or 1)
        cpu = 100.0 * (b["cpu.time"] - a["cpu.time"]) / (dt * 1e9 * max(vcpu, 1))
        if "balloon.available" in b and "balloon.unused" in b:
            usada, origen = (b["balloon.available"] - b["balloon.unused"]) / 1024, "invitado"
        else:
            usada, origen = b.get("balloon.rss", 0.0) / 1024, "rss"
        uso.vms.append(UsoVM(nombre, vcpu, round(max(cpu, 0.0), 1),
                             round(b.get("balloon.maximum", b.get("balloon.current", 0.0)) / 1024, 1),
                             round(usada, 1), origen))
    uso.vms.sort(key=lambda v: v.dominio)
    return uso


# ------------------------------------------------------------------- ejecución
def ejecutor_ssh(timeout_extra_s: float = 30.0) -> Callable[[str, str, float], str]:
    """Ejecutor real: manda el script por stdin a ``ssh <destino> bash -s``."""
    def correr(destino: str, script: str, intervalo_s: float) -> str:
        try:
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", destino, "bash -s"],
                input=script, capture_output=True, text=True,
                timeout=intervalo_s + timeout_extra_s)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ErrorMedicion(f"SSH a {destino} falló: {exc}") from None
        if r.returncode != 0 and f"{SECCION}MEM" not in r.stdout:
            raise ErrorMedicion(f"SSH a {destino} falló (rc={r.returncode}): {r.stderr.strip()[:200]}")
        return r.stdout
    return correr


def destino_ssh(host_id: str, uri: str, entorno: dict | None = None) -> str:
    """``usuario@ip`` para llegar al host por SSH.

    * Linux real: sale de ``qemu+ssh://usuario@ip/system``.
    * OpenStack (``openstack://zona:host``): se usa el nombre del host, o la IP
      indicada en ``ORQ_MONITOR_HOSTS="worker1=10.0.0.11,worker2=..."``; el
      usuario es ``ORQ_SSH_USUARIO`` (por defecto ``ubuntu``).
    """
    env = os.environ if entorno is None else entorno
    if uri.startswith("qemu+ssh://"):
        return uri.split("://", 1)[1].split("/", 1)[0]
    mapa = dict(p.split("=", 1) for p in (env.get("ORQ_MONITOR_HOSTS") or "").split(",") if "=" in p)
    return f"{env.get('ORQ_SSH_USUARIO', 'ubuntu')}@{mapa.get(host_id, host_id)}"


class MedidorUso:
    def __init__(self, ejecutar: Callable[[str, str, float], str] | None = None):
        self._ejecutar = ejecutar or ejecutor_ssh()

    def medir(self, host_id: str, destino: str, intervalo_s: float = 5.0) -> UsoHost:
        salida = self._ejecutar(destino, script_remoto(intervalo_s), intervalo_s)
        return interpretar(salida, host_id)
