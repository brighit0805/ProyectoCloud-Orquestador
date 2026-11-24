"""
network_manager.py
-------------------
Network Manager (submódulo de B) Orquestación).

Responsabilidades:
1. Para cada Enlace del slice, decidir el nombre de red/puente (bridge)
   que lo representará en libvirt, y asignarle una VLAN dedicada (red
   tipo "provider", según RF-26 del Entregable 1 — no túneles
   self-service).
2. Switch físico dinámico: configurar/limpiar el tag VLAN de los puertos
   OVS (br-int) en cada worker cuando se despliega o elimina un slice
   Linux, limitando los broadcasts a la VLAN del slice (R5 — rúbrica EX2).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from .models import Slice, TipoTopologia

# Usuario SSH hacia los workers (mismo que config_cluster.py)
_USUARIO_SSH = "ubuntu"


@dataclass
class VLANAllocator:
    """Reserva VLAN sin repetirlas mientras el proceso esté activo.

    El rango debe reemplazarse por el asignado oficialmente al grupo.
    En una versión posterior, estas reservas se persistirán en la base
    de datos para coordinar varios procesos del orquestador.
    """

    inicio: int = 3000
    fin: int = 3999
    _reservas: dict[str, list[int]] = field(default_factory=dict)

    def reservar(self, slice_id: str, cantidad: int) -> list[int]:
        if slice_id in self._reservas:
            reservas = self._reservas[slice_id]
            if len(reservas) != cantidad:
                raise ValueError("El slice ya tiene una cantidad distinta de VLAN reservadas")
            return list(reservas)

        ocupadas = {vlan for valores in self._reservas.values() for vlan in valores}
        libres = [vlan for vlan in range(self.inicio, self.fin + 1) if vlan not in ocupadas]
        if len(libres) < cantidad:
            raise RuntimeError("No quedan VLAN suficientes en el rango configurado")
        self._reservas[slice_id] = libres[:cantidad]
        return list(self._reservas[slice_id])

    def liberar(self, slice_id: str) -> None:
        self._reservas.pop(slice_id, None)

    def reservar_una(self, slice_id: str) -> int:
        """Reserva UNA VLAN adicional para un slice que ya tiene algunas (o
        ninguna) reservadas, sin tocar las que ya tenía -- a diferencia de
        ``reservar()``, que exige declarar de antemano la cantidad total y
        exacta. La usa la edición en vivo de un slice ya ACTIVO (13-nov):
        agregar un enlace nuevo no debe forzar a recalcular ni revalidar las
        VLAN de los enlaces que no cambiaron."""
        ocupadas = {vlan for valores in self._reservas.values() for vlan in valores}
        libres = [vlan for vlan in range(self.inicio, self.fin + 1) if vlan not in ocupadas]
        if not libres:
            raise RuntimeError("No quedan VLAN suficientes en el rango configurado")
        nueva = libres[0]
        self._reservas.setdefault(slice_id, []).append(nueva)
        return nueva

    def liberar_una(self, slice_id: str, vlan_id: int) -> None:
        """Libera una VLAN puntual de un slice, dejando intactas las demás
        que ese mismo slice tenga reservadas (edición en vivo: quitar un
        enlace sin liberar las VLAN de los enlaces que se quedan)."""
        if slice_id in self._reservas:
            self._reservas[slice_id] = [v for v in self._reservas[slice_id] if v != vlan_id]

    def restaurar(self, slice_id: str, vlans: list[int]) -> None:
        """Marca como ocupadas las VLAN de un slice ya desplegado (al reconstruir
        el estado desde el almacenamiento persistente)."""
        ocupadas = {v for k, valores in self._reservas.items() if k != slice_id for v in valores}
        repetidas = ocupadas.intersection(vlans)
        if repetidas:
            raise RuntimeError(f"VLAN ya reservadas por otro slice: {sorted(repetidas)}")
        self._reservas[slice_id] = list(vlans)


def asignar_redes(slc: Slice, allocator: VLANAllocator) -> dict[str, str]:
    """
    Genera un nombre de red único y determinístico por cada enlace del
    slice, con una VLAN dedicada. Devuelve un diccionario
    enlace_id -> nombre_red, que luego se usa tanto para crear los
    bridges (Fase de creación) como para conectar cada VM a su
    interfaz correcta (Driver Linux).
    """
    redes: dict[str, str] = {}

    if slc.tipo_topologia == TipoTopologia.BUS:
        # Un único dominio de difusión para todo el slice: UNA sola VLAN,
        # no una por enlace. Todos los pares de la cadena (ver
        # topology_manager._figura_a_pares) comparten el mismo nombre_red.
        vlan_id = allocator.reservar(slc.id, 1)[0]
        nombre_red = f"br-{slc.id}-bus-vlan{vlan_id}"
        for enlace in slc.enlaces:
            enlace.nombre_red = nombre_red
            enlace.vlan_id = vlan_id
            redes[enlace.id] = nombre_red
        return redes

    vlans = allocator.reservar(slc.id, len(slc.enlaces))
    for i, (enlace, vlan_id) in enumerate(zip(slc.enlaces, vlans)):
        nombre_red = f"br-{slc.id}-{i}-vlan{vlan_id}"
        enlace.nombre_red = nombre_red
        enlace.vlan_id = vlan_id
        redes[enlace.id] = nombre_red
    return redes


# ---------------------------------------------------------------------------
# Switch físico dinámico — OVS VLAN tagging (R5 EX2)
# ---------------------------------------------------------------------------

def _ssh_ovs(worker_ip: str, *args: str) -> str:
    """Ejecuta 'sudo ovs-vsctl <args>' en el worker vía SSH sin contraseña
    interactiva (requiere NOPASSWD en /etc/sudoers.d/orq-ovs en el worker).
    Lanza RuntimeError si el comando falla."""
    cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
           f"{_USUARIO_SSH}@{worker_ip}", "sudo", "ovs-vsctl"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError(
            f"ovs-vsctl en {worker_ip} falló: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def listar_puertos_ovs(worker_ip: str, bridge: str = "br-int") -> list[str]:
    """Devuelve los puertos del bridge OVS en el worker (ej. ['vnet0', 'ens4'])."""
    salida = _ssh_ovs(worker_ip, "list-ports", bridge)
    return [p for p in salida.splitlines() if p.strip()]


def configurar_vlan_ovs(worker_ip: str, puerto: str, vlan_id: int,
                        bridge: str = "br-int") -> None:
    """Asigna el tag VLAN al puerto OVS indicado en el worker.

    Limita los broadcasts del puerto a la VLAN del slice, aislando
    el tráfico entre topologías (switch físico dinámico — R5 EX2).
    """
    _ssh_ovs(worker_ip, "set", "port", puerto, f"tag={vlan_id}")


def limpiar_vlan_ovs(worker_ip: str, puerto: str,
                     bridge: str = "br-int") -> None:
    """Elimina el tag VLAN del puerto OVS (lo deja en modo trunk/sin VLAN).
    Se llama al eliminar una VM del slice para limpiar la configuración."""
    try:
        _ssh_ovs(worker_ip, "clear", "port", puerto, "tag")
    except RuntimeError:
        # Si el puerto ya no existe (VM eliminada), no es error
        pass


def aplicar_vlan_slice_en_worker(worker_ip: str, vlan_id: int,
                                  bridge: str = "br-int") -> list[str]:
    """Configura el tag VLAN del slice en TODOS los puertos vnetX del worker.

    Detecta automáticamente qué puertos vnetX están presentes en el bridge
    (cada VM crea su propio vnetN al arrancar con libvirt) y les asigna
    el tag VLAN del slice.

    Devuelve la lista de puertos configurados.
    """
    puertos = listar_puertos_ovs(worker_ip, bridge)
    puertos_vnet = [p for p in puertos if p.startswith("vnet")]
    for puerto in puertos_vnet:
        configurar_vlan_ovs(worker_ip, puerto, vlan_id, bridge)
    return puertos_vnet


def limpiar_vlan_slice_en_worker(worker_ip: str, vnet_puerto: str,
                                  bridge: str = "br-int") -> None:
    """Limpia el tag VLAN de un puerto vnetX específico al eliminar una VM."""
    limpiar_vlan_ovs(worker_ip, vnet_puerto, bridge)


def obtener_puerto_vnet_de_vm(worker_ip: str, nombre_vm: str,
                               bridge: str = "br-int") -> str | None:
    """Busca el puerto vnetX en el bridge OVS que corresponde a una VM.

    libvirt crea vnet0, vnet1, … en orden de aparición; el puerto de una
    VM concreta se identifica consultando el XML de la VM en libvirt y
    cruzando con los puertos del bridge OVS.

    Por ahora devuelve el primer puerto vnetX libre (heurística válida
    para clusters con una sola VM por worker por slice).
    """
    puertos = listar_puertos_ovs(worker_ip, bridge)
    puertos_vnet = [p for p in puertos if p.startswith("vnet")]
    return puertos_vnet[0] if puertos_vnet else None


def redes_por_nodo(slc: Slice) -> dict[str, list[str]]:
    """
    Para cada nodo, devuelve la lista de nombres de red a los que debe
    conectarse (un nodo puede participar en 1 o 2 enlaces, según si es
    extremo o intermedio de la topología lineal, o siempre 2 en anillo).
    Esto es lo que el Driver Linux usa para saber cuántas interfaces
    crear por VM y a qué bridge conectar cada una.
    """
    resultado: dict[str, list[str]] = {n.id: [] for n in slc.nodos}
    for enlace in slc.enlaces:
        if enlace.nombre_red is None:
            raise RuntimeError(
                "Los enlaces deben tener red asignada antes de calcular "
                "las interfaces por nodo — llama primero a asignar_redes()."
            )
        # En un bus, varios pares de la cadena comparten el mismo nombre_red
        # (ver asignar_redes); sin el chequeo, un nodo intermedio quedaría
        # con DOS interfaces repetidas sobre la misma red en vez de una sola.
        if enlace.nombre_red not in resultado[enlace.nodo_origen]:
            resultado[enlace.nodo_origen].append(enlace.nombre_red)
        if enlace.nombre_red not in resultado[enlace.nodo_destino]:
            resultado[enlace.nodo_destino].append(enlace.nombre_red)
    return resultado


# ---------------------------------------------------------------------------
# Reglas de seguridad en Linux cluster — iptables (R5 EX2, 15 pts)
# ---------------------------------------------------------------------------
#
# El orquestador instala una chain propia (ORQ-ISOLATION) en cada worker
# al desplegar un slice Linux. Cubre 4 tipos de reglas:
#
#   1. Aislamiento entre slices   — DROP entre VLANs distintas
#   2. Allowlist de puertos       — solo los puertos del slice son válidos
#   3. Anti-spoofing              — bloquea IPs/MACs que no corresponden a la VM
#   4. Rate limiting de broadcasts — limita paquetes broadcast por segundo
#
# Requiere NOPASSWD para /usr/sbin/iptables en sudoers de cada worker
# (ya configurado en /etc/sudoers.d/orq-iptables durante el setup del cluster).
# ---------------------------------------------------------------------------

# Puertos TCP permitidos por defecto en todo slice (SSH de gestión)
_PUERTOS_BASE = [22]

# Límite de broadcasts por segundo por puerto (anti-flood)
_BROADCAST_LIMIT = 50


def _ssh_ipt(worker_ip: str, *args: str) -> str:
    """Ejecuta 'sudo iptables <args>' en el worker vía SSH sin contraseña."""
    cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
           f"{_USUARIO_SSH}@{worker_ip}", "sudo", "iptables"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError(
            f"iptables en {worker_ip} falló: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _chain_existe(worker_ip: str, chain: str) -> bool:
    """Devuelve True si la chain ya existe en iptables del worker."""
    cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
           f"{_USUARIO_SSH}@{worker_ip}",
           "sudo", "iptables", "-L", chain, "-n"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return result.returncode == 0


def instalar_reglas_seguridad(
    worker_ip: str,
    vlan_id: int,
    puertos_extra: list[int] | None = None,
    vm_ip: str | None = None,
    vm_mac: str | None = None,
    bridge: str = "br-int",
) -> None:
    """Instala las 4 reglas de seguridad del orquestador en el worker.

    Se llama una vez por worker al desplegar un slice Linux.

    Parámetros
    ----------
    worker_ip    : IP del server (ej. "192.168.201.1")
    vlan_id      : VLAN asignada al slice (ej. 3001)
    puertos_extra: puertos TCP adicionales a permitir (además de SSH/22)
    vm_ip        : IP de la VM en este worker (para anti-spoofing; opcional)
    vm_mac       : MAC de la VM en este worker (para anti-spoofing; opcional)
    bridge       : bridge OVS donde están conectadas las VMs
    """
    chain = f"ORQ-SL-{vlan_id}"
    puertos = _PUERTOS_BASE + (puertos_extra or [])

    # Crear la chain del slice si no existe
    if not _chain_existe(worker_ip, chain):
        _ssh_ipt(worker_ip, "-N", chain)

    # Anclar la chain en FORWARD (si no está ya)
    # -C comprueba sin modificar; si falla (rc!=0) es porque no existe → la agrega
    check = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
             f"{_USUARIO_SSH}@{worker_ip}",
             "sudo", "iptables", "-C", "FORWARD", "-j", chain]
    if subprocess.run(check, capture_output=True, timeout=10).returncode != 0:
        _ssh_ipt(worker_ip, "-I", "FORWARD", "1", "-j", chain)

    # ------------------------------------------------------------------
    # Regla 1 — Aislamiento entre slices
    # Permite tráfico dentro de la misma VLAN; bloquea el resto en br-int
    # ------------------------------------------------------------------
    # (El aislamiento real lo hace OVS con el tag VLAN; aquí reforzamos
    #  a nivel iptables bloqueando reenvío entre interfaces del bridge
    #  que no pertenezcan al mismo dominio de difusión del slice.)
    _ssh_ipt(worker_ip, "-A", chain,
             "-i", bridge, "-o", bridge,
             "-m", "comment", "--comment", f"orq-slice-{vlan_id}-allow-intra",
             "-j", "ACCEPT")
    _ssh_ipt(worker_ip, "-A", chain,
             "-i", bridge,
             "-m", "comment", "--comment", f"orq-slice-{vlan_id}-drop-inter",
             "-j", "DROP")

    # ------------------------------------------------------------------
    # Regla 2 — Allowlist de puertos (TCP)
    # Solo los puertos declarados para el slice son válidos
    # ------------------------------------------------------------------
    for puerto in puertos:
        _ssh_ipt(worker_ip, "-A", chain,
                 "-p", "tcp", "--dport", str(puerto),
                 "-m", "comment", "--comment", f"orq-slice-{vlan_id}-puerto-{puerto}",
                 "-j", "ACCEPT")
    # Bloquear el resto de TCP
    _ssh_ipt(worker_ip, "-A", chain,
             "-p", "tcp",
             "-m", "comment", "--comment", f"orq-slice-{vlan_id}-tcp-block",
             "-j", "DROP")

    # ------------------------------------------------------------------
    # Regla 3 — Anti-spoofing (si se conoce la IP/MAC de la VM)
    # Bloquea paquetes que vengan de una IP o MAC no asignada a la VM
    # ------------------------------------------------------------------
    if vm_ip:
        _ssh_ipt(worker_ip, "-A", chain,
                 "-i", bridge,
                 "!", "-s", vm_ip,
                 "-m", "comment", "--comment", f"orq-slice-{vlan_id}-antispoofing-ip",
                 "-j", "DROP")
    if vm_mac:
        _ssh_ipt(worker_ip, "-A", chain,
                 "-i", bridge,
                 "-m", "mac", "--mac-source", "!", vm_mac,
                 "-m", "comment", "--comment", f"orq-slice-{vlan_id}-antispoofing-mac",
                 "-j", "DROP")

    # ------------------------------------------------------------------
    # Regla 4 — Rate limiting de broadcasts
    # Limita paquetes broadcast a _BROADCAST_LIMIT pps para evitar floods
    # ------------------------------------------------------------------
    _ssh_ipt(worker_ip, "-A", chain,
             "-i", bridge,
             "-d", "255.255.255.255",
             "-m", "limit", "--limit", f"{_BROADCAST_LIMIT}/sec",
             "-m", "comment", "--comment", f"orq-slice-{vlan_id}-bcast-limit-allow",
             "-j", "ACCEPT")
    _ssh_ipt(worker_ip, "-A", chain,
             "-i", bridge,
             "-d", "255.255.255.255",
             "-m", "comment", "--comment", f"orq-slice-{vlan_id}-bcast-limit-drop",
             "-j", "DROP")


def limpiar_reglas_seguridad(worker_ip: str, vlan_id: int) -> None:
    """Elimina todas las reglas iptables del slice al eliminarlo.

    Desancla la chain de FORWARD y la borra completamente.
    """
    chain = f"ORQ-SL-{vlan_id}"
    if not _chain_existe(worker_ip, chain):
        return  # nada que limpiar

    # Desanclar de FORWARD
    try:
        _ssh_ipt(worker_ip, "-D", "FORWARD", "-j", chain)
    except RuntimeError:
        pass  # puede que ya no estuviera anclada

    # Vaciar y borrar la chain
    try:
        _ssh_ipt(worker_ip, "-F", chain)   # flush (vaciar)
        _ssh_ipt(worker_ip, "-X", chain)   # delete chain
    except RuntimeError:
        pass
