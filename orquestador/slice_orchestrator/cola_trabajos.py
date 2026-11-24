"""
cola_trabajos.py
----------------
Gestor de colas/tareas (submódulo de B) Orquestación, arquitectura 6.2).

R1 pide colas para atender a varios usuarios a la vez. Antes, cada petición
de despliegue o eliminación se ejecutaba dentro de la propia petición: el
usuario quedaba bloqueado hasta que terminaban las VMs, y dos usuarios que
pedían a la vez competían por el cerrojo del servicio sin orden garantizado.

Con la cola:

* La petición solo valida y encola: responde al instante con un trabajo
  (``pendiente``) y su posición en la cola.
* Uno o más hilos trabajadores sacan los trabajos en orden de llegada
  (FIFO) y los ejecutan: ``en_curso`` -> ``completado`` o ``fallido`` (con
  el error concreto).
* Cada usuario puede tener como máximo ``max_pendientes_por_usuario``
  trabajos sin terminar, para que uno solo no acapare la cola.
* No se aceptan dos trabajos activos sobre el mismo slice.

Por defecto hay un solo trabajador: el servicio de slices ya serializa los
cambios de infraestructura con su cerrojo, así que más trabajadores no
ganarían paralelismo y sí romperían el orden FIFO.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger("cola_trabajos")


class EstadoTrabajo(str, Enum):
    PENDIENTE = "pendiente"
    EN_CURSO = "en_curso"
    COMPLETADO = "completado"
    FALLIDO = "fallido"


ACTIVOS = (EstadoTrabajo.PENDIENTE, EstadoTrabajo.EN_CURSO)


class ErrorCola(Exception):
    """Trabajo rechazado al encolar (límite por usuario, slice ocupado...)."""


@dataclass
class Trabajo:
    id: str
    tipo: str                         # "desplegar" | "eliminar"
    usuario: str                      # quien lo pidió
    slice_id: str
    parametros: dict[str, Any] = field(default_factory=dict)
    estado: EstadoTrabajo = EstadoTrabajo.PENDIENTE
    creado: float = 0.0
    iniciado: float | None = None
    terminado: float | None = None
    resultado: Any = None
    error: str | None = None

    def a_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["estado"] = self.estado.value
        return d


class ColaTrabajos:
    def __init__(self, ejecutores: dict[str, Callable[[Trabajo], Any]],
                 trabajadores: int = 1, max_pendientes_por_usuario: int = 5,
                 reloj: Callable[[], float] = time.time, iniciar: bool = True):
        if trabajadores < 1:
            raise ValueError("Se necesita al menos un trabajador")
        self._ejecutores = dict(ejecutores)
        self._max_por_usuario = max_pendientes_por_usuario
        self._reloj = reloj
        self._cola: "queue.Queue[str | None]" = queue.Queue()
        self._trabajos: dict[str, Trabajo] = {}
        self._orden: list[str] = []                # ids en orden de llegada
        self._lock = threading.Lock()
        self._terminado = threading.Condition(self._lock)
        self._contador = itertools.count(1)
        self._hilos: list[threading.Thread] = []
        self._num_trabajadores = trabajadores
        if iniciar:
            self.iniciar()

    # ------------------------------------------------------------ ciclo de vida
    def iniciar(self) -> None:
        if self._hilos:
            return
        for i in range(self._num_trabajadores):
            h = threading.Thread(target=self._bucle, name=f"trabajador-{i + 1}", daemon=True)
            h.start()
            self._hilos.append(h)

    def detener(self, timeout: float = 5.0) -> None:
        for _ in self._hilos:
            self._cola.put(None)
        for h in self._hilos:
            h.join(timeout)
        self._hilos = []

    # ------------------------------------------------------------------ encolar
    def encolar(self, tipo: str, usuario: str, slice_id: str, **parametros: Any) -> dict[str, Any]:
        if tipo not in self._ejecutores:
            raise ErrorCola(f"Tipo de trabajo desconocido: {tipo!r}")
        with self._lock:
            activos = [t for t in self._trabajos.values() if t.estado in ACTIVOS]
            if any(t.slice_id == slice_id for t in activos):
                raise ErrorCola(f"El slice '{slice_id}' ya tiene un trabajo en la cola")
            if sum(1 for t in activos if t.usuario == usuario) >= self._max_por_usuario:
                raise ErrorCola(f"El usuario '{usuario}' ya tiene {self._max_por_usuario} "
                                "trabajos sin terminar; espere a que avancen")
            trabajo = Trabajo(id=f"job-{next(self._contador):04d}", tipo=tipo, usuario=usuario,
                              slice_id=slice_id, parametros=parametros, creado=self._reloj())
            self._trabajos[trabajo.id] = trabajo
            self._orden.append(trabajo.id)
            vista = self._vista(trabajo)
        self._cola.put(trabajo.id)
        logger.info("[%s] encoló %s de %s (%s)", usuario, tipo, slice_id, trabajo.id)
        return vista

    # ------------------------------------------------------------------ consulta
    def _posicion(self, trabajo: Trabajo) -> int | None:
        """1 = el siguiente en ejecutarse. None si ya empezó o terminó."""
        if trabajo.estado != EstadoTrabajo.PENDIENTE:
            return None
        pendientes = [i for i in self._orden
                      if self._trabajos[i].estado == EstadoTrabajo.PENDIENTE]
        return pendientes.index(trabajo.id) + 1

    def _vista(self, trabajo: Trabajo) -> dict[str, Any]:
        d = trabajo.a_dict()
        d["posicion"] = self._posicion(trabajo)
        return d

    def obtener(self, trabajo_id: str) -> dict[str, Any] | None:
        with self._lock:
            t = self._trabajos.get(trabajo_id)
            return self._vista(t) if t else None

    def listar(self, usuario: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [self._vista(self._trabajos[i]) for i in reversed(self._orden)
                    if usuario is None or self._trabajos[i].usuario == usuario]

    def esperar(self, trabajo_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Bloquea hasta que el trabajo termine (para pruebas y para quien
        prefiera el comportamiento síncrono de antes)."""
        limite = None if timeout is None else time.monotonic() + timeout
        with self._terminado:
            while True:
                t = self._trabajos.get(trabajo_id)
                if t is None:
                    raise ErrorCola(f"No existe el trabajo '{trabajo_id}'")
                if t.estado not in ACTIVOS:
                    return self._vista(t)
                restante = None if limite is None else limite - time.monotonic()
                if restante is not None and restante <= 0:
                    raise TimeoutError(f"El trabajo {trabajo_id} sigue '{t.estado.value}'")
                self._terminado.wait(restante)

    # ------------------------------------------------------------------ trabajo
    def _bucle(self) -> None:
        while True:
            trabajo_id = self._cola.get()
            if trabajo_id is None:
                return
            with self._lock:
                trabajo = self._trabajos[trabajo_id]
                trabajo.estado = EstadoTrabajo.EN_CURSO
                trabajo.iniciado = self._reloj()
            try:
                resultado = self._ejecutores[trabajo.tipo](trabajo)
                estado, error = EstadoTrabajo.COMPLETADO, None
            except Exception as exc:          # noqa: BLE001 -- el error queda en el trabajo
                resultado, estado, error = None, EstadoTrabajo.FALLIDO, str(exc)
                logger.warning("Trabajo %s (%s de %s) falló: %s",
                               trabajo.id, trabajo.tipo, trabajo.slice_id, exc)
            with self._terminado:
                trabajo.resultado, trabajo.estado, trabajo.error = resultado, estado, error
                trabajo.terminado = self._reloj()
                self._terminado.notify_all()
            logger.info("Trabajo %s %s", trabajo.id, estado.value)
