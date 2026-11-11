"""
servicio_slices.py
------------------
Capa de servicio que consume la interfaz de usuario (R1B).

Qué es y qué no es
------------------
El HLD (§3.2 y §3.7) separa R1B, que traduce intenciones en peticiones, de R1C,
el Slice Manager que expone la API, autentica y valida. R1C todavía no existe
como API HTTP. Este módulo es su **precursor mínimo**: una clase Python con las
mismas operaciones que el HLD lista (catálogo, crear, editar, listar, borrar,
recursos, imágenes, plantillas, consola) y con las dos reglas que el HLD pone del
lado servidor:

* **Aislamiento por usuario.** Toda operación recibe el ``usuario`` y solo ve y
  toca sus propios slices; un slice ajeno es indistinguible de uno inexistente.
* **Toda validación ocurre aquí, no en la interfaz.** La CLI no valida nada que
  este módulo no vuelva a validar.

Lo que NO hace (ver ``README`` / ``LEEME``): no autentica (no hay JWT ni RBAC; el
usuario es el texto que llega), no encola jobs (las operaciones son síncronas),
no despliega en paralelo (el despliegue se serializa con un cerrojo) y no edita
slices ya activos. Todo eso es R1C / Control de Acceso, y está sin implementar.

Concurrencia
------------
Un cerrojo reentrante protege el estado compartido (slices, inventario de hosts,
VLAN, catálogo de imágenes y tokens). Varios usuarios, desde varios hilos, pueden
usar el mismo ``ServicioSlices`` sin que se pisen los datos. Es concurrencia de
*sesiones*: dos despliegues no corren a la vez. La concurrencia real de despliegue
con reservas atómicas es el diseño de HLD §2.6, aún no implementado.

Persistencia
------------
Si se pasa ``ruta_estado`` el estado se guarda en un JSON (escritura atómica) tras
cada cambio y se recarga al crear el servicio. No es la base de datos del HLD
(§2.4): dos *procesos* escribiendo el mismo archivo se pisarían.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import composicion, direccionamiento, network_manager, serializacion
from .driver_linux import DriverLinux
from .models import (
    CATALOGO_FLAVORS,
    FLAVOR_POR_DEFECTO,
    EstadoSlice,
    Host,
    Slice,
    TipoTopologia,
)
from .orchestrator import DespliegueFallidoError, desplegar_slice_existente, eliminar_slice
from .topology_manager import Injerto, construir_topologia
from .vm_placement import EstrategiaPlacement

logger = logging.getLogger("r1b.servicio")

TTL_TOKEN_CONSOLA_S = 300
PUERTO_VNC_BASE = 5900
_FIRMA_QCOW2 = b"QFI\xfb"

_RE_USUARIO = re.compile(r"^[A-Za-z0-9._@+-]{1,64}$")
_RE_NOMBRE_SLICE = re.compile(r"^[a-z0-9][a-z0-9-]{0,29}$")
_RE_NOMBRE_IMAGEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.qcow2$")

_CAMPOS_CAPACIDAD = {"flavor", "vcpu", "ram_mb", "disco_gb", "imagen"}

# Catálogo de topologías que la interfaz ofrece (HLD §3.4). ``disponible`` dice la
# verdad sobre lo implementado: el bus necesita un segmento compartido que el
# modelo y los drivers aún no representan.
CATALOGO_TOPOLOGIAS: list[dict[str, Any]] = [
    {"tipo": "lineal", "enlaces": "n-1", "minimo_nodos": 2, "restriccion": "n >= 2",
     "disponible": True},
    {"tipo": "anillo", "enlaces": "n", "minimo_nodos": 3,
     "restriccion": "n >= 3 (con 2 nodos el enlace se duplicaría)", "disponible": True},
    {"tipo": "malla", "enlaces": "n(n-1)/2", "minimo_nodos": 3,
     "restriccion": "n >= 3; los enlaces (y las VLAN) crecen de forma cuadrática",
     "disponible": True},
    {"tipo": "arbol", "enlaces": "n-1", "minimo_nodos": 3,
     "restriccion": "n >= 3; requiere el factor de ramificación", "disponible": True},
    {"tipo": "bus", "enlaces": "n", "minimo_nodos": 2,
     "restriccion": "un único dominio de broadcast; necesita un segmento compartido",
     "disponible": False},
]
_TIPOS_DISPONIBLES = {t["tipo"] for t in CATALOGO_TOPOLOGIAS if t["disponible"]}


class ErrorServicio(Exception):
    """Petición rechazada: datos inválidos, estado que no lo permite o conflicto."""


class NoEncontrado(ErrorServicio):
    """El recurso no existe o no pertenece al usuario (no se distingue a propósito)."""


def inventario_simulado() -> list[Host]:
    """Tres workers como los de la EX1 (16 vCPU, 32 GB). Sustituir por el
    inventario real del clúster cuando exista una fuente de verdad (HLD §2.4)."""
    return [Host(f"worker{i}", f"worker{i}", f"sim://worker{i}", 16, 32768, "az-lab")
            for i in (1, 2, 3)]


class ServicioSlices:
    def __init__(self, ruta_estado: str | os.PathLike | None = None,
                 hosts: list[Host] | None = None,
                 driver: Any | None = None,
                 reloj: Callable[[], float] = time.time):
        self._lock = threading.RLock()
        self._ruta = Path(ruta_estado) if ruta_estado else None
        self._hosts_base = hosts if hosts is not None else inventario_simulado()
        self._driver = driver if driver is not None else DriverLinux()
        self._reloj = reloj
        self._slices: dict[str, Slice] = {}
        self._contador = 0
        self._imagenes: dict[str, dict[str, Any]] = {}
        self._tokens: dict[str, dict[str, Any]] = {}
        self._catalogo_inicial()
        self._cargar()

    # ------------------------------------------------------------------
    # Persistencia
    # ------------------------------------------------------------------
    def _catalogo_inicial(self) -> None:
        for f in CATALOGO_FLAVORS.values():
            self._imagenes.setdefault(f.imagen, {
                "nombre": f.imagen, "origen": "catalogo", "formato": "qcow2",
                "tamano_bytes": None, "sha256": None, "importada_por": None})

    def _cargar(self) -> None:
        if self._ruta is None or not self._ruta.exists():
            return
        try:
            datos = json.loads(self._ruta.read_text(encoding="utf-8"))
            self._contador = int(datos["contador"])
            self._slices = {d["id"]: serializacion.slice_desde_dict(d) for d in datos["slices"]}
            self._imagenes.update({d["nombre"]: d for d in datos["imagenes"]})
            self._tokens = dict(datos["tokens"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ErrorServicio(
                f"No se pudo leer el archivo de estado {self._ruta}: {exc}. "
                "Revíselo o bórrelo para empezar de cero.") from None

    def _guardar(self) -> None:
        if self._ruta is None:
            return
        datos = {
            "version": 1,
            "contador": self._contador,
            "slices": [serializacion.slice_a_dict(s) for s in self._slices.values()],
            "imagenes": list(self._imagenes.values()),
            "tokens": self._tokens,
        }
        self._ruta.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._ruta.parent), prefix=".estado-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(datos, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._ruta)          # atómico: nunca queda un JSON a medias
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ------------------------------------------------------------------
    # Utilidades internas
    # ------------------------------------------------------------------
    @staticmethod
    def _usuario(usuario: Any) -> str:
        if not isinstance(usuario, str) or not _RE_USUARIO.match(usuario):
            raise ErrorServicio("Usuario inválido: use 1 a 64 caracteres (letras, números, . _ @ + -)")
        return usuario

    def _propio(self, usuario: str, slice_id: str) -> Slice:
        slc = self._slices.get(slice_id)
        if slc is None or slc.owner != usuario:
            raise NoEncontrado(f"El slice '{slice_id}' no existe o no le pertenece")
        return slc

    def _nuevo_id(self) -> str:
        self._contador += 1
        return f"slice-{self._contador:03d}"

    def _inventario(self) -> list[Host]:
        """Hosts con el uso que dejan los slices activos (se recalcula siempre)."""
        hosts = [Host(**{**h.__dict__, "vcpu_usados": 0, "ram_usados_mb": 0})
                 for h in self._hosts_base]
        por_id = {h.id: h for h in hosts}
        for slc in self._slices.values():
            if slc.estado != EstadoSlice.ACTIVO:
                continue
            for n in slc.nodos:
                h = por_id.get(n.host_id) if n.host_id else None
                if h is not None:
                    h.vcpu_usados += n.vcpu
                    h.ram_usados_mb += n.ram_mb
        return hosts

    def _allocator(self) -> network_manager.VLANAllocator:
        alloc = network_manager.VLANAllocator()
        for slc in self._slices.values():
            if slc.estado == EstadoSlice.ACTIVO:
                alloc.restaurar(slc.id, [e.vlan_id for e in slc.enlaces if e.vlan_id is not None])
        return alloc

    def _validar_imagenes(self, slc: Slice) -> None:
        for n in slc.nodos:
            if n.imagen not in self._imagenes:
                raise ErrorServicio(
                    f"La imagen '{n.imagen}' de {composicion.clave_de(n)} no está en el catálogo. "
                    "Impórtela primero con 'imagen importar'.")

    def _validar_zona(self, zona: Any) -> None:
        if zona is None:
            return
        zonas = {h.zona_disponibilidad for h in self._hosts_base}
        if not isinstance(zona, str) or zona not in zonas:
            raise ErrorServicio(f"La zona '{zona}' no existe. Zonas: {', '.join(sorted(zonas))}")

    # ------------------------------------------------------------------
    # Área A: diseño del slice
    # ------------------------------------------------------------------
    def listar_topologias(self) -> list[dict[str, Any]]:
        return copy.deepcopy(CATALOGO_TOPOLOGIAS)

    def listar_flavors(self) -> list[dict[str, Any]]:
        return [{"nombre": f.nombre, "vcpu": f.vcpu, "ram_mb": f.ram_mb,
                 "disco_gb": f.disco_gb, "imagen": f.imagen}
                for f in CATALOGO_FLAVORS.values()]

    def _construir(self, slice_id: str, nombre: str, usuario: str, spec: dict) -> Slice:
        top = spec.get("topologia")
        if not isinstance(top, dict):
            raise ErrorServicio("Falta 'topologia' (un objeto con la topología base)")
        base = top.get("base")
        if isinstance(base, dict):                 # forma de HLD §3.5
            tipo_txt, nodos, ramif = base.get("tipo"), base.get("nodos"), base.get("ramificacion", 2)
        else:                                      # forma de HLD §3.7: "base": "anillo", "nodos": 4
            tipo_txt, nodos, ramif = base, top.get("nodos"), top.get("ramificacion", 2)
        if tipo_txt == "bus":
            raise ErrorServicio("La topología 'bus' está diseñada pero no implementada: "
                                "el modelo aún no representa un segmento compartido")
        if tipo_txt not in _TIPOS_DISPONIBLES:
            raise ErrorServicio(f"Topología '{tipo_txt}' no válida. Disponibles: "
                                f"{', '.join(sorted(_TIPOS_DISPONIBLES))}")
        if not isinstance(nodos, int) or isinstance(nodos, bool):
            raise ErrorServicio("'nodos' debe ser un número entero")
        if not isinstance(ramif, int) or isinstance(ramif, bool):
            raise ErrorServicio("'ramificacion' debe ser un número entero")
        if nodos > composicion.MAX_NODOS:
            raise ErrorServicio(f"El máximo es {composicion.MAX_NODOS} nodos por slice")

        injertos_json = list(top.get("injertos") or [])
        for r in top.get("ramas") or []:           # alias de HLD §3.7
            injertos_json.append({"sobre": r.get("desde"), "tipo": r.get("tipo"),
                                  "nodos": r.get("nodos"), "ramificacion": r.get("ramificacion", 2)})
        injertos = []
        for j in injertos_json:
            if not isinstance(j, dict) or j.get("tipo") not in _TIPOS_DISPONIBLES:
                raise ErrorServicio("Cada injerto necesita 'sobre', 'tipo' (del catálogo) y 'nodos'")
            if not isinstance(j.get("nodos"), int) or not isinstance(j.get("sobre"), str):
                raise ErrorServicio("Cada injerto necesita 'sobre' (texto) y 'nodos' (entero)")
            injertos.append(Injerto(sobre=j["sobre"], tipo=TipoTopologia(j["tipo"]),
                                    num_nodos=j["nodos"], ramificacion=j.get("ramificacion", 2)))

        flavores = spec.get("flavores") or {}
        acceso = spec.get("acceso_internet") or []
        if not isinstance(flavores, dict) or not isinstance(acceso, list):
            raise ErrorServicio("'flavores' debe ser un objeto y 'acceso_internet' una lista")

        slc = construir_topologia(
            slice_id=slice_id, nombre=nombre, owner=usuario,
            tipo=TipoTopologia(tipo_txt), num_nodos=nodos,
            flavor_map=flavores, internet_access=acceso,
            injertos=injertos or None, ramificacion=ramif)
        slc.estado = EstadoSlice.BORRADOR
        # Los nodos que no nombran un flavor salen de construir_topologia con la imagen
        # genérica heredada de los primeros laboratorios; aquí toman el flavor por defecto.
        for n in slc.nodos:
            if n.imagen == "generic-linux.qcow2":
                composicion.aplicar_capacidad(n, flavor=FLAVOR_POR_DEFECTO)

        capacidad = spec.get("capacidad") or {}
        if not isinstance(capacidad, dict):
            raise ErrorServicio("'capacidad' debe ser un objeto {nodo: {vcpu, ram_mb, disco_gb, imagen}}")
        for clave, valores in capacidad.items():
            self._aplicar_capacidad(slc, clave, valores)

        # edición libre del grafo (HLD §3.5)
        editado = False
        for par in spec["topologia"].get("enlaces_extra") or []:
            composicion.agregar_enlace(slc, *self._par(par))
            editado = True
        for par in spec["topologia"].get("enlaces_quitados") or []:
            composicion.quitar_enlace(slc, *self._par(par))
            editado = True
        if editado:
            slc.tipo_topologia = composicion.clasificar(slc)

        zona = spec.get("zona_disponibilidad")
        self._validar_zona(zona)
        slc.zona_disponibilidad = zona
        return slc

    @staticmethod
    def _par(par: Any) -> tuple[str, str]:
        if not (isinstance(par, (list, tuple)) and len(par) == 2
                and all(isinstance(x, str) for x in par)):
            raise ErrorServicio("Un enlace se indica como un par de nodos, p. ej. [\"vm2\", \"vm5\"]")
        return par[0], par[1]

    @staticmethod
    def _aplicar_capacidad(slc: Slice, clave: str, valores: Any) -> None:
        if not isinstance(valores, dict) or not valores:
            raise ErrorServicio(f"La capacidad de '{clave}' debe ser un objeto no vacío")
        desconocidos = set(valores) - _CAMPOS_CAPACIDAD
        if desconocidos:
            raise ErrorServicio(f"Campos de capacidad desconocidos: {', '.join(sorted(desconocidos))}")
        for campo, tipo in (("vcpu", int), ("ram_mb", int)):
            if campo in valores and (not isinstance(valores[campo], tipo) or isinstance(valores[campo], bool)):
                raise ErrorServicio(f"'{campo}' debe ser un número entero")
        if "disco_gb" in valores and (not isinstance(valores["disco_gb"], (int, float))
                                      or isinstance(valores["disco_gb"], bool)):
            raise ErrorServicio("'disco_gb' debe ser un número")
        composicion.aplicar_capacidad(composicion.resolver(slc, clave), **valores)

    def crear_slice(self, usuario: str, spec: dict, desplegar: bool = False,
                    estrategia: str = "balanceado") -> dict[str, Any]:
        """Crea un slice BORRADOR (sin reservar recursos) y, si se pide, lo despliega."""
        usuario = self._usuario(usuario)
        with self._lock:
            if not isinstance(spec, dict):
                raise ErrorServicio("La especificación debe ser un objeto JSON")
            nombre = spec.get("nombre")
            if not isinstance(nombre, str) or not _RE_NOMBRE_SLICE.match(nombre):
                raise ErrorServicio("Nombre inválido: use minúsculas, números y guiones (1 a 30)")
            if any(s.owner == usuario and s.nombre == nombre for s in self._slices.values()):
                raise ErrorServicio(f"Ya tiene un slice llamado '{nombre}'")
            contador_previo = self._contador
            slice_id = self._nuevo_id()
            try:
                slc = self._construir(slice_id, nombre, usuario, spec)
                composicion.exigir_valido(slc)
                self._validar_imagenes(slc)
            except ValueError as exc:
                self._contador = contador_previo
                raise ErrorServicio(str(exc)) from None
            except ErrorServicio:
                self._contador = contador_previo
                raise
            self._slices[slice_id] = slc
            self._guardar()
            logger.info("[%s] creó %s (%s, %d nodos)", usuario, slice_id, nombre, len(slc.nodos))
            if desplegar:
                return self.desplegar_slice(usuario, slice_id, estrategia)
            return self.ver_slice(usuario, slice_id)

    def editar_slice(self, usuario: str, slice_id: str, operaciones: list[dict]) -> dict[str, Any]:
        """Aplica una lista de operaciones de composición, todas o ninguna.

        Operaciones: agregar_nodo, agregar_enlace, quitar_enlace, eliminar_nodo,
        injertar, capacidad, acceso_internet. Solo se editan slices en BORRADOR o
        ERROR; la edición incremental de un slice activo está diseñada, no implementada.
        """
        usuario = self._usuario(usuario)
        with self._lock:
            slc = self._propio(usuario, slice_id)
            if slc.estado not in (EstadoSlice.BORRADOR, EstadoSlice.ERROR):
                raise ErrorServicio(
                    f"El slice está '{slc.estado.value}': solo se edita en BORRADOR o ERROR. "
                    "Exporte su plantilla, elimínelo y cree uno nuevo desde ella.")
            if not isinstance(operaciones, list) or not operaciones:
                raise ErrorServicio("Indique al menos una operación de edición")
            trabajo = copy.deepcopy(slc)               # si algo falla, el original no se toca
            try:
                for op in operaciones:
                    self._aplicar_operacion(trabajo, op)
                self._validar_imagenes(trabajo)
            except ValueError as exc:
                raise ErrorServicio(str(exc)) from None
            trabajo.estado = EstadoSlice.BORRADOR
            for n in trabajo.nodos:
                n.host_id = None
            for e in trabajo.enlaces:
                e.vlan_id = None
                e.nombre_red = None
            self._slices[slice_id] = trabajo
            self._guardar()
            logger.info("[%s] editó %s (%d operaciones)", usuario, slice_id, len(operaciones))
            return self.ver_slice(usuario, slice_id)

    def _aplicar_operacion(self, slc: Slice, op: Any) -> None:
        if not isinstance(op, dict) or "op" not in op:
            raise ErrorServicio("Cada operación debe ser un objeto con el campo 'op'")
        nombre = op["op"]
        if nombre == "agregar_nodo":
            capacidad = op.get("capacidad")
            if capacidad is not None:
                if not isinstance(capacidad, dict) or set(capacidad) - _CAMPOS_CAPACIDAD:
                    raise ErrorServicio("Capacidad inválida en agregar_nodo")
            composicion.agregar_nodo(slc, flavor=op.get("flavor"), capacidad=capacidad,
                                     acceso_internet=bool(op.get("acceso_internet", False)))
        elif nombre == "agregar_enlace":
            composicion.agregar_enlace(slc, *self._par([op.get("a"), op.get("b")]))
        elif nombre == "quitar_enlace":
            composicion.quitar_enlace(slc, *self._par([op.get("a"), op.get("b")]))
        elif nombre == "eliminar_nodo":
            if not isinstance(op.get("nodo"), str):
                raise ErrorServicio("eliminar_nodo necesita 'nodo'")
            composicion.eliminar_nodo(slc, op["nodo"])
        elif nombre == "injertar":
            if op.get("tipo") not in _TIPOS_DISPONIBLES:
                raise ErrorServicio(f"Tipo de injerto inválido. Disponibles: {', '.join(sorted(_TIPOS_DISPONIBLES))}")
            if not isinstance(op.get("sobre"), str) or not isinstance(op.get("nodos"), int):
                raise ErrorServicio("injertar necesita 'sobre' (texto) y 'nodos' (entero)")
            composicion.injertar(slc, op["sobre"], TipoTopologia(op["tipo"]), op["nodos"],
                                 ramificacion=op.get("ramificacion", 2), flavor=op.get("flavor"))
        elif nombre == "capacidad":
            valores = {k: v for k, v in op.items() if k not in ("op", "nodo")}
            if not isinstance(op.get("nodo"), str):
                raise ErrorServicio("capacidad necesita 'nodo'")
            self._aplicar_capacidad(slc, op["nodo"], valores)
        elif nombre == "acceso_internet":
            if not isinstance(op.get("nodo"), str) or not isinstance(op.get("valor"), bool):
                raise ErrorServicio("acceso_internet necesita 'nodo' y 'valor' (verdadero/falso)")
            composicion.resolver(slc, op["nodo"]).acceso_internet = op["valor"]
        else:
            raise ErrorServicio(f"Operación desconocida: '{nombre}'")

    # ------------------------------------------------------------------
    # Área B: ciclo de vida
    # ------------------------------------------------------------------
    def desplegar_slice(self, usuario: str, slice_id: str,
                        estrategia: str = "balanceado") -> dict[str, Any]:
        usuario = self._usuario(usuario)
        with self._lock:
            slc = self._propio(usuario, slice_id)
            if slc.estado not in (EstadoSlice.BORRADOR, EstadoSlice.ERROR):
                raise ErrorServicio(f"El slice ya está '{slc.estado.value}'")
            try:
                est = EstrategiaPlacement(estrategia)
            except ValueError:
                raise ErrorServicio("Estrategia inválida: use 'balanceado' o 'round_robin'") from None
            problemas = composicion.validar(slc)
            if problemas:
                raise ErrorServicio("El diseño no es desplegable: " + "; ".join(problemas))
            self._validar_imagenes(slc)
            try:
                desplegar_slice_existente(slc, self._inventario(), self._driver,
                                          self._allocator(), est, reintentos=1,
                                          espera_reintento_s=0.0)
            except (DespliegueFallidoError, ValueError) as exc:
                slc.estado = EstadoSlice.ERROR
                self._guardar()
                logger.error("[%s] despliegue de %s falló: %s", usuario, slice_id, exc)
                raise ErrorServicio(f"No se pudo desplegar: {exc}") from None
            self._guardar()
            logger.info("[%s] desplegó %s", usuario, slice_id)
            return self.ver_slice(usuario, slice_id)

    def listar_slices(self, usuario: str) -> list[dict[str, Any]]:
        usuario = self._usuario(usuario)
        with self._lock:
            return [{
                "id": s.id, "nombre": s.nombre, "estado": s.estado.value,
                "tipo": s.tipo_topologia.value, "nodos": len(s.nodos),
                "enlaces": len(s.enlaces), "zona": s.zona_disponibilidad,
            } for s in self._slices.values() if s.owner == usuario]

    def ver_slice(self, usuario: str, slice_id: str) -> dict[str, Any]:
        usuario = self._usuario(usuario)
        with self._lock:
            slc = self._propio(usuario, slice_id)
            activo = slc.estado == EstadoSlice.ACTIVO
            nodos = []
            for n in slc.nodos:
                interfaces: list[dict[str, Any]] = []
                if activo:
                    interfaces = [{"enlace": i.enlace_id, "vlan": i.vlan, "ip": i.cidr}
                                  for i in direccionamiento.interfaces_de(slc, n)]
                nodos.append({
                    "clave": composicion.clave_de(n), "nombre": n.nombre, "flavor": n.flavor,
                    "vcpu": n.vcpu, "ram_mb": n.ram_mb, "disco_gb": n.disco_gb,
                    "imagen": n.imagen, "host": n.host_id, "estado": n.estado,
                    "acceso_internet": n.acceso_internet, "interfaces": interfaces,
                })
            claves = {n.id: composicion.clave_de(n) for n in slc.nodos}
            enlaces = [{"id": e.id, "a": claves.get(e.nodo_origen), "b": claves.get(e.nodo_destino),
                        "vlan": e.vlan_id, "red": e.nombre_red} for e in slc.enlaces]
            return {"id": slc.id, "nombre": slc.nombre, "owner": slc.owner,
                    "estado": slc.estado.value, "tipo": slc.tipo_topologia.value,
                    "zona": slc.zona_disponibilidad, "nodos": nodos, "enlaces": enlaces}

    def eliminar_slice(self, usuario: str, slice_id: str) -> dict[str, Any]:
        usuario = self._usuario(usuario)
        with self._lock:
            slc = self._propio(usuario, slice_id)
            if slc.estado == EstadoSlice.ACTIVO:
                eliminar_slice(slc, self._inventario(), self._driver, self._allocator())
            del self._slices[slice_id]
            self._tokens = {h: t for h, t in self._tokens.items() if t["slice_id"] != slice_id}
            self._guardar()
            logger.info("[%s] eliminó %s", usuario, slice_id)
            return {"id": slice_id, "estado": EstadoSlice.ELIMINADO.value}

    # ------------------------------------------------------------------
    # Plantillas (HLD §3.6)
    # ------------------------------------------------------------------
    def exportar_plantilla(self, usuario: str, slice_id: str) -> dict[str, Any]:
        usuario = self._usuario(usuario)
        with self._lock:
            return serializacion.plantilla_de(self._propio(usuario, slice_id))

    def crear_desde_plantilla(self, usuario: str, plantilla: Any,
                              nombre: str | None = None) -> dict[str, Any]:
        """Crea un BORRADOR a partir de una plantilla; los recursos no se comparten."""
        usuario = self._usuario(usuario)
        with self._lock:
            if nombre is None and isinstance(plantilla, dict):
                nombre = plantilla.get("nombre_sugerido")
            if not isinstance(nombre, str) or not _RE_NOMBRE_SLICE.match(nombre):
                raise ErrorServicio("Nombre inválido: use minúsculas, números y guiones (1 a 30)")
            if any(s.owner == usuario and s.nombre == nombre for s in self._slices.values()):
                raise ErrorServicio(f"Ya tiene un slice llamado '{nombre}'")
            contador_previo = self._contador
            slice_id = self._nuevo_id()
            try:
                slc = serializacion.slice_desde_plantilla(
                    plantilla, slice_id, nombre, usuario, set(self._imagenes),
                    {h.zona_disponibilidad for h in self._hosts_base})
            except ValueError as exc:
                self._contador = contador_previo
                raise ErrorServicio(str(exc)) from None
            self._slices[slice_id] = slc
            self._guardar()
            logger.info("[%s] creó %s desde una plantilla", usuario, slice_id)
            return self.ver_slice(usuario, slice_id)

    # ------------------------------------------------------------------
    # Área C: recursos e imágenes
    # ------------------------------------------------------------------
    def consumo_recursos(self, usuario: str) -> dict[str, Any]:
        """Uso del clúster por host (agregado, sin detalle de otros usuarios) y de
        los slices del propio usuario."""
        usuario = self._usuario(usuario)
        with self._lock:
            hosts = self._inventario()
            disco_por_host: dict[str, float] = {h.id: 0.0 for h in hosts}
            for s in self._slices.values():
                if s.estado == EstadoSlice.ACTIVO:
                    for n in s.nodos:
                        if n.host_id in disco_por_host:
                            disco_por_host[n.host_id] += n.disco_gb
            salida_hosts = [{
                "host": h.id, "zona": h.zona_disponibilidad,
                "vcpu_usados": h.vcpu_usados, "vcpu_total": h.capacidad_vcpu,
                "ram_usada_mb": h.ram_usados_mb, "ram_total_mb": h.capacidad_ram_mb,
                "disco_usado_gb": round(disco_por_host[h.id], 2),
            } for h in hosts]
            mios = []
            total = {"vcpu": 0, "ram_mb": 0, "disco_gb": 0.0}
            for s in self._slices.values():
                if s.owner != usuario or s.estado != EstadoSlice.ACTIVO:
                    continue
                v = sum(n.vcpu for n in s.nodos)
                r = sum(n.ram_mb for n in s.nodos)
                d = round(sum(n.disco_gb for n in s.nodos), 2)
                mios.append({"id": s.id, "nombre": s.nombre, "nodos": len(s.nodos),
                             "vcpu": v, "ram_mb": r, "disco_gb": d})
                total["vcpu"] += v
                total["ram_mb"] += r
                total["disco_gb"] = round(total["disco_gb"] + d, 2)
            return {"hosts": salida_hosts, "mis_slices": mios, "total_mio": total}

    def listar_imagenes(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(i) for i in self._imagenes.values()]

    def importar_imagen(self, usuario: str, nombre: str, origen: str) -> dict[str, Any]:
        """Registra una imagen qcow2 en el catálogo tras validar el archivo.

        Valida el nombre, que el archivo exista y que empiece con la firma qcow2, y
        guarda su tamaño y SHA-256. NO sube la imagen al repositorio de imágenes del
        servidor ni a los workers: en el despliegue real el driver la copia por nombre
        desde ese repositorio, así que el archivo debe estar ya allí.
        """
        usuario = self._usuario(usuario)
        with self._lock:
            if not isinstance(nombre, str) or not _RE_NOMBRE_IMAGEN.match(nombre):
                raise ErrorServicio("Nombre de imagen inválido: use letras, números, . _ - y termine en .qcow2")
            if nombre in self._imagenes:
                raise ErrorServicio(f"La imagen '{nombre}' ya está en el catálogo")
            ruta = Path(str(origen))
            if not ruta.is_file():
                raise ErrorServicio(f"No se encontró el archivo de origen: {origen}")
            try:
                with ruta.open("rb") as f:
                    if f.read(4) != _FIRMA_QCOW2:
                        raise ErrorServicio("El archivo no es una imagen qcow2 (firma incorrecta)")
                    f.seek(0)
                    h = hashlib.sha256()
                    for bloque in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(bloque)
                tamano = ruta.stat().st_size
            except OSError as exc:
                raise ErrorServicio(f"No se pudo leer el archivo: {exc}") from None
            registro = {"nombre": nombre, "origen": str(ruta), "formato": "qcow2",
                        "tamano_bytes": tamano, "sha256": h.hexdigest(), "importada_por": usuario}
            self._imagenes[nombre] = registro
            self._guardar()
            logger.info("[%s] importó la imagen %s (%d bytes)", usuario, nombre, tamano)
            return dict(registro)

    # ------------------------------------------------------------------
    # Área D: acceso a consola (HLD §3.8, §5.5)
    # ------------------------------------------------------------------
    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _purgar_tokens(self) -> None:
        ahora = self._reloj()
        self._tokens = {h: t for h, t in self._tokens.items() if t["expira"] > ahora}

    def emitir_token_consola(self, usuario: str, slice_id: str, nodo: str) -> dict[str, Any]:
        """Emite un token de un solo uso, nominativo y con caducidad para la consola
        de una VM del usuario. Se guarda solo su hash. El puerto es simulado: el
        proxy VNC del headnode del HLD no existe todavía."""
        usuario = self._usuario(usuario)
        with self._lock:
            slc = self._propio(usuario, slice_id)
            if slc.estado != EstadoSlice.ACTIVO:
                raise ErrorServicio("Solo se emite consola de slices activos")
            try:
                n = composicion.resolver(slc, nodo)
            except ValueError as exc:
                raise ErrorServicio(str(exc)) from None
            self._purgar_tokens()
            token = secrets.token_urlsafe(32)
            posicion = slc.nodos.index(n)
            self._tokens[self._hash_token(token)] = {
                "usuario": usuario, "slice_id": slice_id, "nodo": composicion.clave_de(n),
                "host": n.host_id, "puerto": PUERTO_VNC_BASE + posicion,
                "expira": self._reloj() + TTL_TOKEN_CONSOLA_S,
            }
            self._guardar()
            logger.info("[%s] emitió token de consola de %s/%s", usuario, slice_id, composicion.clave_de(n))
            return {"host": n.host_id, "puerto": PUERTO_VNC_BASE + posicion, "token": token,
                    "expira_en": TTL_TOKEN_CONSOLA_S, "simulado": True}

    def canjear_token_consola(self, token: str) -> dict[str, Any]:
        """Valida y CONSUME un token. Lo usaría el proxy de consola; aquí sirve para
        demostrar que es de un solo uso y que caduca."""
        with self._lock:
            self._purgar_tokens()
            registro = self._tokens.pop(self._hash_token(str(token)), None)
            if registro is None:
                raise ErrorServicio("Token inválido, ya usado o caducado")
            self._guardar()
            logger.info("[%s] canjeó el token de consola de %s/%s",
                        registro["usuario"], registro["slice_id"], registro["nodo"])
            return {k: registro[k] for k in ("usuario", "slice_id", "nodo", "host", "puerto")}
