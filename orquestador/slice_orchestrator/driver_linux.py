"""Driver Linux explícito para simulación o ejecución con libvirt.

La simulación es el modo predeterminado y nunca cambia por sí sola a modo
real. Esto evita tocar accidentalmente los servidores por el solo hecho de
tener instalado ``libvirt-python``.

Qué hace en modo REAL al crear una VM:
  1. deriva su disco QCOW2 de la imagen base (backing file);
  2. arma la «semilla» cloud-init (IPs, usuario) y la sube al pool como un
     CD-ROM virtual;
  3. define el dominio con una MAC fija por interfaz y lo arranca.
Al eliminarla borra el dominio, su disco derivado y su semilla; la imagen
base se conserva.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time

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
    bridges_por_host: dict[str, str] = field(default_factory=dict)
    pool_almacenamiento: str = "default"
    reintentos_conexion: int = 3
    espera_reintento_s: float = 2.0
    redes_simuladas: set[tuple[str, str]] = field(default_factory=set)
    vms_simuladas: set[tuple[str, str]] = field(default_factory=set)
    # (host_uri, nombre_vm, nombre_red) -> (vlan_id, mac) de cada interfaz
    # agregada en vivo (conectar_interfaz/desconectar_interfaz, 13-nov). Se
    # guarda también en modo REAL porque libvirt exige la MISMA definición
    # de interfaz (mac/vlan/bridge) para poder desconectarla después.
    interfaces_conectadas: dict[tuple[str, str, str], tuple[int | None, str | None]] = field(
        default_factory=dict)
    semillas_simuladas: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    historial: list[tuple[str, str, str]] = field(default_factory=list)
    fallar_en_operacion: int | None = None
    fallas_consecutivas: int = 1          # cuántas operaciones seguidas fallan (simulación)
    repo_imagenes: str = "imagenes"       # carpeta con las imágenes base (en quien orquesta)
    estado_imagenes_path: str | None = None   # JSON con las copias que creó el orquestador
    imagenes_simuladas: set[tuple[str, str]] = field(default_factory=set)
    vm_imagen_simulada: dict[tuple[str, str], str] = field(default_factory=dict)
    _creadas_memoria: set[tuple[str, str]] = field(default_factory=set)
    _numero_operacion: int = 0

    # ------------------------------------------------------------ utilidades
    def _registrar(self, accion: str, host_uri: str, recurso: str) -> None:
        self._numero_operacion += 1
        n = self._numero_operacion
        if (self.fallar_en_operacion is not None
                and self.fallar_en_operacion <= n < self.fallar_en_operacion + self.fallas_consecutivas):
            raise DriverLinuxError(f"Falla simulada en {accion} de {recurso}")
        self.historial.append((accion, host_uri, recurso))

    def _bridge_para(self, host_uri: str) -> str:
        """Devuelve el bridge OVS correspondiente al worker."""
        return self.bridges_por_host.get(host_uri, self.bridge_provider)

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
        """Abre la conexión libvirt con reintentos: un SSH que se corta una vez
        no debe tumbar todo el despliegue."""
        libvirt = self._libvirt()
        ultimo: Exception | None = None
        for intento in range(1, self.reintentos_conexion + 1):
            try:
                conn = libvirt.open(uri)
                if conn is not None:
                    return conn
            except Exception as exc:       # noqa: BLE001
                ultimo = exc
            logger.warning("Conexión a %s falló (intento %d/%d): %s",
                           uri, intento, self.reintentos_conexion, ultimo)
            time.sleep(self.espera_reintento_s)
        raise DriverLinuxError(f"No se pudo conectar a {uri}: {ultimo}")

    # ----------------------------------------------------------------- redes
    def crear_red(self, host_uri: str, nombre_red: str, vlan_id: int) -> str:
        self._registrar("crear_red", host_uri, nombre_red)
        if self.modo == ModoDriver.SIMULADO:
            self.redes_simuladas.add((host_uri, nombre_red))
            logger.info("[SIMULADO] crear_red host=%s red=%s vlan=%s", host_uri, nombre_red, vlan_id)
            return nombre_red

        # La VLAN se aplica en cada interfaz de VM sobre el bridge OVS del
        # worker; no hay nada más que crear en el host.
        logger.info("[REAL] red lógica %s usa el bridge del worker, VLAN %s", nombre_red, vlan_id)
        return nombre_red

    def eliminar_red(self, host_uri: str, nombre_red: str) -> None:
        self._registrar("eliminar_red", host_uri, nombre_red)
        if self.modo == ModoDriver.SIMULADO:
            self.redes_simuladas.discard((host_uri, nombre_red))
            logger.info("[SIMULADO] eliminar_red host=%s red=%s", host_uri, nombre_red)

    # ------------------------------------------------------------------ XML
    @staticmethod
    def _interfaz_xml(vlan: int | None, mac: str | None, bridge: str) -> str:
        """XML libvirt de una interfaz de red sobre el bridge OVS del
        worker. Factorizado (13-nov) para que lo use tanto la definición
        inicial de la VM (``_xml_vm``) como conectar/desconectar_interfaz en
        caliente -- libvirt exige la MISMA definición para poder
        desconectar después una interfaz que se conectó así."""
        etiqueta = f"<vlan><tag id='{vlan}'/></vlan>" if vlan is not None else ""
        m = f"<mac address='{mac}'/>" if mac else ""
        return (f"<interface type='bridge'>{m}<source bridge='{bridge}'/>"
                f"<virtualport type='openvswitch'/>{etiqueta}"
                f"<model type='virtio'/></interface>")

    def _xml_vm(self, nombre_vm: str, vcpu: int, ram_mb: int,
                volumen: str, redes: list[tuple[str, int]],
                bridge: str | None = None,
                macs: list[str] | None = None,
                semilla: str | None = None,
                acceso_vlan: int | None = None,
                con_acceso: bool = False,
                mac_acceso: str | None = None,
                red_libvirt: str | None = None) -> str:
        bridge = bridge or self.bridge_provider

        def itf(vlan: int | None, mac: str | None) -> str:
            return self._interfaz_xml(vlan, mac, bridge)

        macs = macs or []
        interfaces = [itf(vlan_id, macs[i] if i < len(macs) else None)
                      for i, (_, vlan_id) in enumerate(redes)]
        if con_acceso:
            if red_libvirt:      # NAT de libvirt (red 'default'): salida a Internet sin tocar el headnode
                m = f"<mac address='{mac_acceso}'/>" if mac_acceso else ""
                interfaces.append(f"<interface type='network'>{m}<source network='{red_libvirt}'/>"
                                  f"<model type='virtio'/></interface>")
            else:
                interfaces.append(itf(acceso_vlan, mac_acceso))
        cdrom = ""
        if semilla:
            cdrom = (f"<disk type='file' device='cdrom'><driver name='qemu' type='raw'/>"
                     f"<source file='{semilla}'/><target dev='hdc' bus='ide'/><readonly/></disk>")
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
            {cdrom}
            {''.join(interfaces)}
            <graphics type='vnc' port='-1' autoport='yes'/>
          </devices>
        </domain>"""

    # --------------------------------------------------------------- discos
    def _crear_volumen(self, conn, nombre_vm: str, disco_gb: float, imagen: str) -> str:
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
        capacidad_bytes = int(disco_gb * 1024 ** 3)
        xml = f"""<volume>
          <name>{nombre_volumen}</name>
          <capacity unit='bytes'>{capacidad_bytes}</capacity>
          <target><format type='qcow2'/></target>
          <backingStore><path>{base.path()}</path><format type='qcow2'/></backingStore>
        </volume>"""
        return pool.createXML(xml, 0).path()

    @staticmethod
    def construir_iso_semilla(archivos: dict[str, str]) -> bytes:
        """Empaqueta user-data / meta-data / network-config en una ISO cloud-init
        (volumen «cidata»). Usa cloud-localds o genisoimage, lo que haya."""
        with tempfile.TemporaryDirectory() as tmp:
            for nombre, texto in archivos.items():
                with open(os.path.join(tmp, nombre), "w") as f:
                    f.write(texto)
            iso = os.path.join(tmp, "seed.iso")
            if shutil.which("cloud-localds"):
                cmd = ["cloud-localds"]
                if "network-config" in archivos:
                    cmd += [f"--network-config={os.path.join(tmp, 'network-config')}"]
                cmd += [iso, os.path.join(tmp, "user-data"), os.path.join(tmp, "meta-data")]
            else:
                herramienta = shutil.which("genisoimage") or shutil.which("mkisofs")
                if herramienta is None:
                    raise DriverLinuxError(
                        "Falta cloud-localds o genisoimage para crear la semilla. "
                        "Instale: sudo apt install -y cloud-image-utils")
                cmd = [herramienta, "-output", iso, "-volid", "cidata", "-joliet", "-rock"] + \
                      [os.path.join(tmp, n) for n in archivos]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise DriverLinuxError(f"No se pudo crear la ISO de semilla: {r.stderr.strip()}")
            with open(iso, "rb") as f:
                return f.read()

    def _subir_semilla(self, conn, nombre_vm: str, contenido: bytes) -> str:
        """Sube la ISO al pool de libvirt (sin necesitar sudo en el worker)."""
        pool = conn.storagePoolLookupByName(self.pool_almacenamiento)
        nombre = f"seed-{nombre_vm}.iso"
        try:
            pool.storageVolLookupByName(nombre).delete(0)      # regenerar siempre
        except Exception:
            pass
        xml = (f"<volume><name>{nombre}</name><capacity unit='bytes'>{len(contenido)}</capacity>"
               f"<target><format type='raw'/></target></volume>")
        vol = pool.createXML(xml, 0)
        flujo = conn.newStream(0)
        vol.upload(flujo, 0, len(contenido), 0)
        flujo.send(contenido)
        flujo.finish()
        return vol.path()

    # ------------------------------------------------------------------- VMs
    def crear_vm(self, host_uri: str, nombre_vm: str, vcpu: int, ram_mb: int,
                 disco_gb: float, imagen: str, redes: list[tuple[str, int]],
                 macs: list[str] | None = None,
                 semilla: dict[str, str] | None = None,
                 acceso: dict | None = None) -> str:
        """``macs``: una MAC por entrada de ``redes``. ``semilla``: archivos
        cloud-init. ``acceso``: {"vlan": int|None, "mac": str} si la VM lleva
        una interfaz extra hacia el headnode (salida a Internet)."""
        self._registrar("crear_vm", host_uri, nombre_vm)
        if self.modo == ModoDriver.SIMULADO:
            self.vms_simuladas.add((host_uri, nombre_vm))
            self.vm_imagen_simulada[(host_uri, nombre_vm)] = imagen
            if semilla is not None:
                self.semillas_simuladas[(host_uri, nombre_vm)] = semilla
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
            ruta_semilla = None
            if semilla:
                ruta_semilla = self._subir_semilla(conn, nombre_vm, self.construir_iso_semilla(semilla))
            dom = conn.defineXML(self._xml_vm(
                nombre_vm, vcpu, ram_mb, volumen, redes, self._bridge_para(host_uri),
                macs=macs, semilla=ruta_semilla,
                acceso_vlan=(acceso or {}).get("vlan"), con_acceso=acceso is not None,
                mac_acceso=(acceso or {}).get("mac"),
                red_libvirt=(acceso or {}).get("red_libvirt")))
            dom.create()
            return nombre_vm
        finally:
            conn.close()

    def eliminar_vm(self, host_uri: str, nombre_vm: str) -> None:
        self._registrar("eliminar_vm", host_uri, nombre_vm)
        if self.modo == ModoDriver.SIMULADO:
            self.vms_simuladas.discard((host_uri, nombre_vm))
            self.vm_imagen_simulada.pop((host_uri, nombre_vm), None)
            self.semillas_simuladas.pop((host_uri, nombre_vm), None)
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
            pool = conn.storagePoolLookupByName(self.pool_almacenamiento)
            for vol in (f"{nombre_vm}.qcow2", f"seed-{nombre_vm}.iso"):
                try:
                    pool.storageVolLookupByName(vol).delete(0)
                except libvirt.libvirtError:
                    pass          # ya no existe: la operación es idempotente
        finally:
            conn.close()

    # ------------------------------------------ edición en vivo (13-nov)
    def conectar_interfaz(self, host_uri: str, nombre_vm: str, nombre_red: str,
                          vlan_id: int, mac: str | None = None, ip: str | None = None) -> None:
        """Agrega una interfaz a una VM YA CORRIENDO, sin recrearla (edición
        en vivo de un slice ACTIVO). ``nombre_red`` debe existir de antemano
        (``crear_red()``). ``ip`` no se usa aquí: a diferencia de OpenStack
        (donde la IP se fija en el puerto de Neutron), en libvirt la IP la
        configura la propia VM (cloud-init/DHCP), no la interfaz de red."""
        self._registrar("conectar_interfaz", host_uri, f"{nombre_vm}:{nombre_red}")
        clave = (host_uri, nombre_vm, nombre_red)
        if self.modo == ModoDriver.SIMULADO:
            self.interfaces_conectadas[clave] = (vlan_id, mac)
            logger.info("[SIMULADO] conectar_interfaz vm=%s red=%s vlan=%s", nombre_vm, nombre_red, vlan_id)
            return

        libvirt = self._libvirt()
        conn = self._conectar(host_uri)
        try:
            dom = conn.lookupByName(nombre_vm)
            xml = self._interfaz_xml(vlan_id, mac, self._bridge_para(host_uri))
            dom.attachDeviceFlags(xml, libvirt.VIR_DOMAIN_AFFECT_LIVE | libvirt.VIR_DOMAIN_AFFECT_CONFIG)
            self.interfaces_conectadas[clave] = (vlan_id, mac)
        finally:
            conn.close()

    def desconectar_interfaz(self, host_uri: str, nombre_vm: str, nombre_red: str) -> None:
        """Inversa de ``conectar_interfaz``. Idempotente: no falla si la
        interfaz (o la VM) ya no existen."""
        self._registrar("desconectar_interfaz", host_uri, f"{nombre_vm}:{nombre_red}")
        clave = (host_uri, nombre_vm, nombre_red)
        if self.modo == ModoDriver.SIMULADO:
            self.interfaces_conectadas.pop(clave, None)
            logger.info("[SIMULADO] desconectar_interfaz vm=%s red=%s", nombre_vm, nombre_red)
            return

        vlan_id, mac = self.interfaces_conectadas.get(clave, (None, None))
        libvirt = self._libvirt()
        conn = self._conectar(host_uri)
        try:
            try:
                dom = conn.lookupByName(nombre_vm)
            except libvirt.libvirtError:
                return      # la VM ya no existe: nada que desconectar
            xml = self._interfaz_xml(vlan_id, mac, self._bridge_para(host_uri))
            try:
                dom.detachDeviceFlags(xml, libvirt.VIR_DOMAIN_AFFECT_LIVE | libvirt.VIR_DOMAIN_AFFECT_CONFIG)
            except libvirt.libvirtError:
                pass        # ya estaba desconectada: operación idempotente
            self.interfaces_conectadas.pop(clave, None)
        finally:
            conn.close()

    # ------------------------------------------------- imágenes base (R2)
    # El orquestador copia la imagen base a un worker solo si hace falta y la
    # borra cuando ya ninguna VM de ese worker depende de ella («borrado
    # inteligente»). Solo borra las copias que ÉL creó, nunca las que ya
    # estaban en el worker.

    def _leer_creadas(self) -> set[tuple[str, str]]:
        if self.estado_imagenes_path and os.path.exists(self.estado_imagenes_path):
            with open(self.estado_imagenes_path) as f:
                return {(h, i) for h, i in json.load(f)}
        return set(self._creadas_memoria)

    def _guardar_creadas(self, creadas: set[tuple[str, str]]) -> None:
        self._creadas_memoria = set(creadas)
        if self.estado_imagenes_path:
            with open(self.estado_imagenes_path, "w") as f:
                json.dump(sorted(creadas), f)

    def asegurar_imagen(self, host_uri: str, imagen: str) -> bool:
        """Garantiza que la imagen base exista en el worker. Devuelve True si
        la tuvo que copiar, False si ya estaba. Idempotente."""
        if self.modo == ModoDriver.SIMULADO:
            if (host_uri, imagen) in self.imagenes_simuladas:
                return False
            self.imagenes_simuladas.add((host_uri, imagen))
            self._guardar_creadas(self._leer_creadas() | {(host_uri, imagen)})
            logger.info("[SIMULADO] copiar imagen %s a %s", imagen, host_uri)
            return True

        libvirt = self._libvirt()
        conn = self._conectar(host_uri)
        try:
            pool = conn.storagePoolLookupByName(self.pool_almacenamiento)
            pool.refresh(0)
            try:
                pool.storageVolLookupByName(imagen)
                return False
            except libvirt.libvirtError:
                pass
            origen = os.path.join(self.repo_imagenes, imagen)
            if not os.path.isfile(origen):
                raise DriverLinuxError(f"No existe la imagen base '{origen}' para copiarla al worker")
            tam = os.path.getsize(origen)
            capacidad = max(tam, self._tamano_virtual(origen))
            xml = (f"<volume><name>{imagen}</name><capacity unit='bytes'>{capacidad}</capacity>"
                   f"<target><format type='qcow2'/></target></volume>")
            vol = pool.createXML(xml, 0)
            flujo = conn.newStream(0)
            vol.upload(flujo, 0, tam, 0)
            with open(origen, "rb") as f:
                flujo.sendAll(lambda st, nbytes, fd: fd.read(nbytes), f)
            flujo.finish()
            self._guardar_creadas(self._leer_creadas() | {(host_uri, imagen)})
            logger.info("[REAL] imagen %s copiada a %s (%d bytes)", imagen, host_uri, tam)
            return True
        finally:
            conn.close()

    @staticmethod
    def _tamano_virtual(ruta: str) -> int:
        """Tamaño virtual del disco según qemu-img (0 si no está disponible)."""
        if not shutil.which("qemu-img"):
            return 0
        r = subprocess.run(["qemu-img", "info", "--output=json", ruta],
                           capture_output=True, text=True)
        try:
            return int(json.loads(r.stdout)["virtual-size"])
        except Exception:      # noqa: BLE001
            return 0

    def liberar_imagen_si_huerfana(self, host_uri: str, imagen: str) -> bool:
        """Borra la copia de la imagen base en el worker SI la creó el
        orquestador y NINGUNA VM de ese worker depende ya de ella.
        Devuelve True si la borró."""
        creadas = self._leer_creadas()
        if (host_uri, imagen) not in creadas:
            return False

        if self.modo == ModoDriver.SIMULADO:
            if any(h == host_uri and img == imagen for (h, _), img in self.vm_imagen_simulada.items()):
                return False
            self.imagenes_simuladas.discard((host_uri, imagen))
            self._guardar_creadas(creadas - {(host_uri, imagen)})
            logger.info("[SIMULADO] borrar imagen huérfana %s en %s", imagen, host_uri)
            return True

        import xml.etree.ElementTree as ET
        libvirt = self._libvirt()
        conn = self._conectar(host_uri)
        try:
            pool = conn.storagePoolLookupByName(self.pool_almacenamiento)
            pool.refresh(0)
            try:
                base = pool.storageVolLookupByName(imagen)
            except libvirt.libvirtError:
                self._guardar_creadas(creadas - {(host_uri, imagen)})
                return False
            ruta_base = base.path()
            for vol in pool.listAllVolumes():
                if vol.name() == imagen:
                    continue
                for ruta in ET.fromstring(vol.XMLDesc(0)).iter("path"):
                    if ruta.text == ruta_base:
                        return False          # una VM todavía depende de la base
            base.delete(0)
            self._guardar_creadas(creadas - {(host_uri, imagen)})
            logger.info("[REAL] imagen base %s eliminada de %s (ya sin VMs que la usen)", imagen, host_uri)
            return True
        finally:
            conn.close()
