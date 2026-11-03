"""Driver Linux explícito para simulación o ejecución con libvirt.

La simulación es el modo predeterminado y nunca cambia por sí sola a modo
real. Esto evita tocar accidentalmente los servidores por el solo hecho de
tener instalado ``libvirt-python``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import logging

logger = logging.getLogger("driver_linux")


class ModoDriver(str, Enum):
    SIMULADO = "simulado"
    REAL = "real"


class DriverLinuxError(Exception):
    pass


@dataclass
class DriverLinux:
    modo: ModoDriver = ModoDriver.SIMULADO
    bridge_provider: str = "br-provider"
    pool_almacenamiento: str = "default"
    redes_simuladas: set[tuple[str, str]] = field(default_factory=set)
    vms_simuladas: set[tuple[str, str]] = field(default_factory=set)
    historial: list[tuple[str, str, str]] = field(default_factory=list)
    fallar_en_operacion: int | None = None
    _numero_operacion: int = 0

    def _registrar(self, accion: str, host_uri: str, recurso: str) -> None:
        self._numero_operacion += 1
        if self.fallar_en_operacion == self._numero_operacion:
            raise DriverLinuxError(f"Falla simulada en {accion} de {recurso}")
        self.historial.append((accion, host_uri, recurso))

    def _libvirt(self):
        if self.modo != ModoDriver.REAL:
            return None
        try:
            import libvirt  # type: ignore
        except ImportError as exc:
            raise DriverLinuxError(
                "El modo real requiere libvirt-python; use modo simulado para las pruebas locales"
            ) from exc
        return libvirt

    def _conectar(self, uri: str):
        libvirt = self._libvirt()
        try:
            conn = libvirt.open(uri)
        except Exception as exc:
            raise DriverLinuxError(f"No se pudo conectar a {uri}: {exc}") from exc
        if conn is None:
            raise DriverLinuxError(f"No se pudo abrir la conexión libvirt con {uri}")
        return conn

    def crear_red(self, host_uri: str, nombre_red: str, vlan_id: int) -> str:
        self._registrar("crear_red", host_uri, nombre_red)
        if self.modo == ModoDriver.SIMULADO:
            self.redes_simuladas.add((host_uri, nombre_red))
            logger.info("[SIMULADO] crear_red host=%s red=%s vlan=%s", host_uri, nombre_red, vlan_id)
            return nombre_red

        # La VLAN se aplica en cada interfaz de VM sobre el bridge OVS
        # compartido. La existencia del bridge se valida en laboratorio.
        logger.info("[REAL] red lógica %s usa %s VLAN %s", nombre_red, self.bridge_provider, vlan_id)
        return nombre_red

    def eliminar_red(self, host_uri: str, nombre_red: str) -> None:
        self._registrar("eliminar_red", host_uri, nombre_red)
        if self.modo == ModoDriver.SIMULADO:
            self.redes_simuladas.discard((host_uri, nombre_red))
            logger.info("[SIMULADO] eliminar_red host=%s red=%s", host_uri, nombre_red)

    def _xml_vm(self, nombre_vm: str, vcpu: int, ram_mb: int,
                volumen: str, redes: list[tuple[str, int]]) -> str:
        interfaces = "\n".join(
            f"""<interface type='bridge'>
              <source bridge='{self.bridge_provider}'/>
              <virtualport type='openvswitch'/>
              <vlan><tag id='{vlan_id}'/></vlan>
              <model type='virtio'/>
            </interface>"""
            for _, vlan_id in redes
        )
        return f"""<domain type='kvm'>
          <name>{nombre_vm}</name>
          <memory unit='MiB'>{ram_mb}</memory>
          <vcpu>{vcpu}</vcpu>
          <os><type arch='x86_64'>hvm</type></os>
          <devices>
            <disk type='file' device='disk'>
              <driver name='qemu' type='qcow2'/>
              <source file='{volumen}'/>
              <target dev='vda' bus='virtio'/>
            </disk>
            {interfaces}
            <graphics type='vnc' port='-1' autoport='yes'/>
          </devices>
        </domain>"""

    def _crear_volumen(self, conn, nombre_vm: str, disco_gb: int, imagen: str) -> str:
        pool = conn.storagePoolLookupByName(self.pool_almacenamiento)
        pool.refresh(0)
        nombre_volumen = f"{nombre_vm}.qcow2"
        try:
            return pool.storageVolLookupByName(nombre_volumen).path()
        except Exception:
            pass
        try:
            base = pool.storageVolLookupByName(imagen)
        except Exception as exc:
            raise DriverLinuxError(
                f"La imagen base '{imagen}' no existe en el pool '{self.pool_almacenamiento}'"
            ) from exc
        xml = f"""<volume>
          <name>{nombre_volumen}</name>
          <capacity unit='GiB'>{disco_gb}</capacity>
          <target><format type='qcow2'/></target>
          <backingStore><path>{base.path()}</path><format type='qcow2'/></backingStore>
        </volume>"""
        return pool.createXML(xml, 0).path()

    def crear_vm(self, host_uri: str, nombre_vm: str, vcpu: int, ram_mb: int,
                 disco_gb: int, imagen: str, redes: list[tuple[str, int]]) -> str:
        self._registrar("crear_vm", host_uri, nombre_vm)
        if self.modo == ModoDriver.SIMULADO:
            self.vms_simuladas.add((host_uri, nombre_vm))
            logger.info("[SIMULADO] crear_vm host=%s vm=%s redes=%s", host_uri, nombre_vm, redes)
            return nombre_vm

        libvirt = self._libvirt()
        conn = self._conectar(host_uri)
        try:
            try:
                dom = conn.lookupByName(nombre_vm)
                if not dom.isActive():
                    dom.create()
                return nombre_vm
            except libvirt.libvirtError:
                pass
            volumen = self._crear_volumen(conn, nombre_vm, disco_gb, imagen)
            dom = conn.defineXML(self._xml_vm(nombre_vm, vcpu, ram_mb, volumen, redes))
            dom.create()
            return nombre_vm
        finally:
            conn.close()

    def eliminar_vm(self, host_uri: str, nombre_vm: str) -> None:
        self._registrar("eliminar_vm", host_uri, nombre_vm)
        if self.modo == ModoDriver.SIMULADO:
            self.vms_simuladas.discard((host_uri, nombre_vm))
            logger.info("[SIMULADO] eliminar_vm host=%s vm=%s", host_uri, nombre_vm)
            return

        libvirt = self._libvirt()
        conn = self._conectar(host_uri)
        try:
            try:
                dom = conn.lookupByName(nombre_vm)
            except libvirt.libvirtError:
                dom = None
            if dom is not None:
                if dom.isActive():
                    dom.destroy()
                dom.undefine()
            try:
                pool = conn.storagePoolLookupByName(self.pool_almacenamiento)
                pool.storageVolLookupByName(f"{nombre_vm}.qcow2").delete(0)
            except libvirt.libvirtError:
                pass
        finally:
            conn.close()
