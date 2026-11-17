# -*- coding: utf-8 -*-
"""
Aplica el fix del 13-nov: agrega descubrir_hosts() al driver Linux y la
capacidad de activarlo en modo REAL desde el CLI (ORQ_LINUX_REAL=1).

Uso (desde la carpeta 'orquestador', en el headnode):
    python3 aplicar_driver_linux_real.py

No requiere red ni permisos especiales -- solo edita dos archivos locales:
  - slice_orchestrator/driver_linux.py
  - slice_orchestrator/cli.py

Es idempotente: si ya se aplicó, lo detecta y no hace nada (ni se queja).
"""
import sys

CAMBIOS = []

def editar(ruta, viejo, nuevo, nombre):
    with open(ruta, "r", encoding="utf-8") as f:
        contenido = f.read()
    if nuevo in contenido:
        print(f"[ya aplicado] {nombre}")
        return
    if viejo not in contenido:
        print(f"[ERROR] No encontré el texto esperado para '{nombre}' en {ruta}. "
              f"¿El archivo cambió? Avísale a Claude para ajustar el parche.")
        sys.exit(1)
    contenido = contenido.replace(viejo, nuevo, 1)
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(contenido)
    print(f"[aplicado] {nombre}")


# ---------------------------------------------------------- driver_linux.py
RUTA_DRIVER = "slice_orchestrator/driver_linux.py"

editar(
    RUTA_DRIVER,
    viejo='''import subprocess
import tempfile
import time

logger = logging.getLogger("driver_linux")''',
    nuevo='''import subprocess
import tempfile
import time

from .models import Host

logger = logging.getLogger("driver_linux")''',
    nombre="import de Host en driver_linux.py",
)

editar(
    RUTA_DRIVER,
    viejo='''    _creadas_memoria: set[tuple[str, str]] = field(default_factory=set)
    _numero_operacion: int = 0''',
    nuevo='''    _creadas_memoria: set[tuple[str, str]] = field(default_factory=set)
    _numero_operacion: int = 0
    # Inventario de hosts reales que este driver sabe consultar por libvirt
    # (hallazgo del 13-nov: antes no existía -- el CLI nunca podía activar el
    # modo REAL porque nada sabía CUÁLES eran los workers ni cómo conectarse).
    # Solo se usa en modo REAL; en modo SIMULADO queda vacío sin efecto.
    hosts_conocidos: list[Host] = field(default_factory=list)''',
    nombre="campo hosts_conocidos en DriverLinux",
)

editar(
    RUTA_DRIVER,
    viejo='''            logger.info("[REAL] imagen base %s eliminada de %s (ya sin VMs que la usen)", imagen, host_uri)
            return True
        finally:
            conn.close()''',
    nuevo='''            logger.info("[REAL] imagen base %s eliminada de %s (ya sin VMs que la usen)", imagen, host_uri)
            return True
        finally:
            conn.close()

    # ----------------------------------------------------- monitoreo (Lab 7)
    def descubrir_hosts(self) -> list[Host]:
        """Lee cuántos vCPU/RAM están REALMENTE en uso en cada host de
        ``hosts_conocidos``, sumando TODOS los dominios activos de libvirt
        (no solo los que creó este orquestador -- así el ResourceMonitor ve
        lo mismo que vería un administrador con 'virsh list').

        Hallazgo del 13-nov: este método no existía. El ResourceMonitor lo
        necesita (ver scheduler.ProveedorEstado) y, al no encontrarlo,
        fallaba en silencio y el Scheduler usaba el inventario interno del
        orquestador en vez de leer libvirt de verdad -- por eso las pruebas
        con --driver linux hasta hoy habían sido simulación, no validación
        real, aunque el mensaje de error no era obvio.

        En modo SIMULADO no hay nada externo que leer -- se avisa con un
        error claro en vez de devolver números inventados; el
        ResourceMonitor ya sabe caer de vuelta al inventario interno ante
        cualquier excepción (ver scheduler.ResourceMonitor.refrescar)."""
        if self.modo == ModoDriver.SIMULADO:
            raise DriverLinuxError(
                "descubrir_hosts() solo lee estado real vía libvirt; en modo SIMULADO "
                "no hay nada externo que leer (el monitor usa el inventario interno)")
        libvirt = self._libvirt()
        resultado: list[Host] = []
        for host in self.hosts_conocidos:
            conn = self._conectar(host.uri_libvirt)
            try:
                vcpu_usados = 0
                ram_usados_mb = 0
                for dom_id in conn.listDomainsID():
                    try:
                        dom = conn.lookupByID(dom_id)
                        _estado, _maxmem_kb, memoria_kb, vcpus, _cputime = dom.info()
                    except libvirt.libvirtError:
                        continue    # el dominio desapareció entre listar y leer: se ignora
                    vcpu_usados += vcpus
                    ram_usados_mb += memoria_kb // 1024
            finally:
                conn.close()
            resultado.append(Host(
                id=host.id, nombre=host.nombre, uri_libvirt=host.uri_libvirt,
                capacidad_vcpu=host.capacidad_vcpu, capacidad_ram_mb=host.capacidad_ram_mb,
                zona_disponibilidad=host.zona_disponibilidad,
                vcpu_usados=vcpu_usados, ram_usados_mb=ram_usados_mb,
                capacidad_disco_gb=host.capacidad_disco_gb,
                activo=host.activo,
                overcommit_vcpu=host.overcommit_vcpu, overcommit_ram=host.overcommit_ram,
            ))
        return resultado''',
    nombre="método descubrir_hosts() en DriverLinux",
)

# ------------------------------------------------------------------ cli.py
RUTA_CLI = "slice_orchestrator/cli.py"

editar(
    RUTA_CLI,
    viejo='''from .servicio_slices import ErrorServicio, ServicioSlices


def _construir_servicio_openstack_real(ruta_estado: str) -> ServicioSlices:''',
    nuevo='''from .models import Host
from .servicio_slices import ErrorServicio, ServicioSlices


def _construir_servicio_linux_real(ruta_estado: str) -> ServicioSlices:
    """Arma un ServicioSlices contra el clúster REAL Linux/KVM (libvirt vía
    SSH a cada worker), usando las IPs de 'config_cluster.py' (en la raíz de
    'orquestador/'). Requiere libvirt-python instalado en el headnode y SSH
    sin contraseña (por llave) hacia cada worker.

    Se activa con ORQ_LINUX_REAL=1 además de --driver linux (o ORQ_DRIVER=
    linux); sin esa variable, --driver linux sigue siendo el simulado de
    siempre, para no tocar nada por accidente."""
    try:
        import config_cluster as cfg_cluster
    except ImportError as exc:
        raise ErrorServicio(
            "ORQ_LINUX_REAL=1 pide 'config_cluster.py' (debe existir en la carpeta "
            "'orquestador', al mismo nivel que 'slice_orchestrator/'). No se encontró."
        ) from exc
    from .driver_linux import DriverLinux, ModoDriver

    hosts_config = [
        Host(nombre, nombre, cfg_cluster.uri(nombre), 16, 32768, "az-lab")
        for nombre in cfg_cluster.WORKERS
    ]
    driver = DriverLinux(modo=ModoDriver.REAL, hosts_conocidos=hosts_config,
                          pool_almacenamiento=getattr(cfg_cluster, "POOL_LIBVIRT", "default"),
                          bridges_por_host={cfg_cluster.uri(n): cfg_cluster.WORKERS[n]["bridge"]
                                            for n in cfg_cluster.WORKERS})
    try:
        hosts_reales = driver.descubrir_hosts()
    except Exception as exc:  # cualquier fallo de red/SSH/libvirt se traduce a un error de CLI limpio
        raise ErrorServicio(f"No se pudo descubrir el inventario real del clúster Linux: {exc}") from exc
    print("⚠ Usando el driver Linux REAL -- las operaciones afectan a los workers físicos.",
          file=sys.stderr)
    return ServicioSlices(ruta_estado=ruta_estado, hosts=hosts_reales, driver=driver)


def _construir_servicio_openstack_real(ruta_estado: str) -> ServicioSlices:''',
    nombre="función _construir_servicio_linux_real() en cli.py",
)

editar(
    RUTA_CLI,
    viejo='''        elif args.driver == "openstack":
            svc = _construir_servicio_openstack_real(args.estado)
        else:
            svc = ServicioSlices(ruta_estado=args.estado)''',
    nuevo='''        elif args.driver == "openstack":
            svc = _construir_servicio_openstack_real(args.estado)
        elif args.driver == "linux" and os.environ.get("ORQ_LINUX_REAL"):
            svc = _construir_servicio_linux_real(args.estado)
        else:
            svc = ServicioSlices(ruta_estado=args.estado)''',
    nombre="activación de ORQ_LINUX_REAL en main()",
)

print("\nListo. Ahora corre:  python3 -m pytest tests/ -q")
print("Para activar el modo real:  ORQ_LINUX_REAL=1 python3 -m slice_orchestrator.cli --driver linux recursos")
