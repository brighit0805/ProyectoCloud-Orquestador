"""
persistencia_sqlite.py
----------------------
Almacén durable del orquestador sobre SQLite (HLD §6.4, dominio C: Persistencia).

Por qué existe
--------------
Hasta ahora el estado (slices, catálogo de imágenes, tokens de consola) vivía en
un único JSON con escritura atómica. Funciona para un proceso, pero no se puede
consultar ("¿qué slices hay desplegados?", "¿qué corre en worker2?") sin cargar y
recorrer todo el archivo, y dos *procesos* (la CLI y la API HTTP, por ejemplo)
que lo escriban a la vez se pisan. German Espinoza lo señaló (23-nov-2026) y
sugirió una base de datos.

Qué hace
--------
* ``slices``: una fila por slice, con las columnas consultables (dueño, estado,
  tipo, zona) y el slice completo serializado en ``datos`` (fuente de verdad para
  reconstruir el modelo sin pérdida).
* ``nodos`` y ``enlaces``: proyección relacional de cada slice (host, vCPU, RAM,
  disco, VLAN...). Se reescribe junto con el slice, en la misma transacción.
  Sirve para consultas SQL e inventario; el servicio no depende de ella para
  cargar.
* ``imagenes`` y ``tokens``: catálogo de imágenes y tokens de consola (solo el
  hash del token, igual que antes).
* ``meta``: contador de IDs (``slice-NNN``) y marca de migración.
* Cada ``guardar`` es UNA transacción ``BEGIN IMMEDIATE``: o se aplica todo o
  nada. Solo se escriben las filas que cambiaron respecto de lo último que este
  proceso leyó o escribió, de modo que otro proceso que haya tocado *otros*
  slices no queda sobrescrito.
* El contador de IDs se incrementa dentro de la base (``siguiente_id``), así que
  dos procesos nunca reciben el mismo ``slice-NNN``.
* Modo WAL: lecturas concurrentes mientras otro proceso escribe.

Qué NO hace
-----------
No arbitra reservas de capacidad entre procesos: dos despliegues simultáneos en
procesos distintos pueden decidir el mismo host con el inventario que cada uno
leyó. Eso es el diseño de reservas atómicas del HLD §2.6, aún pendiente; la
concurrencia entre hilos de un mismo proceso sigue resuelta por el cerrojo de
``ServicioSlices``.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("r1b.sqlite")

VERSION_ESQUEMA = 1
EXTENSIONES_SQLITE = (".db", ".sqlite", ".sqlite3")

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS meta (
    clave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slices (
    id                  TEXT PRIMARY KEY,
    nombre              TEXT NOT NULL,
    owner               TEXT NOT NULL,
    estado              TEXT NOT NULL,
    tipo_topologia      TEXT NOT NULL,
    zona_disponibilidad TEXT,
    datos               TEXT NOT NULL,
    actualizado         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_slices_owner  ON slices(owner);
CREATE INDEX IF NOT EXISTS ix_slices_estado ON slices(estado);
CREATE TABLE IF NOT EXISTS nodos (
    slice_id      TEXT NOT NULL REFERENCES slices(id) ON DELETE CASCADE,
    id            TEXT NOT NULL,
    nombre        TEXT NOT NULL,
    host_id       TEXT,
    vcpu          INTEGER NOT NULL,
    ram_mb        INTEGER NOT NULL,
    disco_gb      REAL NOT NULL,
    imagen        TEXT NOT NULL,
    flavor        TEXT,
    estado        TEXT,
    ip            TEXT,
    acceso_internet INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (slice_id, id)
);
CREATE INDEX IF NOT EXISTS ix_nodos_host ON nodos(host_id);
CREATE TABLE IF NOT EXISTS enlaces (
    slice_id     TEXT NOT NULL REFERENCES slices(id) ON DELETE CASCADE,
    id           TEXT NOT NULL,
    nodo_origen  TEXT NOT NULL,
    nodo_destino TEXT NOT NULL,
    nombre_red   TEXT,
    vlan_id      INTEGER,
    PRIMARY KEY (slice_id, id)
);
CREATE INDEX IF NOT EXISTS ix_enlaces_vlan ON enlaces(vlan_id);
CREATE TABLE IF NOT EXISTS imagenes (
    nombre TEXT PRIMARY KEY,
    datos  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    hash  TEXT PRIMARY KEY,
    datos TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS v_slices_desplegados AS
    SELECT s.id, s.nombre, s.owner, s.estado, s.tipo_topologia,
           s.zona_disponibilidad,
           (SELECT COUNT(*) FROM nodos n WHERE n.slice_id = s.id)   AS nodos,
           (SELECT COUNT(*) FROM enlaces e WHERE e.slice_id = s.id) AS enlaces
      FROM slices s
     WHERE s.estado = 'activo';
"""


def es_ruta_sqlite(ruta: str | os.PathLike | None) -> bool:
    """True si la ruta pide el almacén SQLite (por su extensión)."""
    return ruta is not None and str(ruta).lower().endswith(EXTENSIONES_SQLITE)


def _json(obj: Any) -> str:
    # sort_keys: la misma estructura siempre produce el mismo texto, así la
    # comparación "¿cambió?" no depende del orden de las claves.
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ErrorAlmacen(Exception):
    """No se pudo leer o escribir el archivo de base de datos."""


class AlmacenSQLite:
    def __init__(self, ruta: str | os.PathLike, timeout_s: float = 30.0):
        self._ruta = Path(ruta)
        self._lock = threading.RLock()
        # Lo último que ESTE proceso leyó o escribió: base del diff de guardar().
        self._ultimo: dict[str, dict[str, str]] = {"slices": {}, "imagenes": {}, "tokens": {}}
        try:
            self._ruta.parent.mkdir(parents=True, exist_ok=True)
            # isolation_level=None: las transacciones se manejan a mano (BEGIN IMMEDIATE).
            self._con = sqlite3.connect(str(self._ruta), timeout=timeout_s,
                                        isolation_level=None, check_same_thread=False)
            self._con.row_factory = sqlite3.Row
            self._con.execute("PRAGMA foreign_keys = ON")
            self._con.execute("PRAGMA journal_mode = WAL")
            self._con.execute("PRAGMA synchronous = NORMAL")
            self._con.executescript(_ESQUEMA)
            self._con.execute("INSERT OR IGNORE INTO meta(clave, valor) VALUES('version_esquema', ?)",
                              (str(VERSION_ESQUEMA),))
            self._con.execute("INSERT OR IGNORE INTO meta(clave, valor) VALUES('contador', '0')")
        except (sqlite3.Error, OSError) as exc:
            raise ErrorAlmacen(f"No se pudo abrir la base de datos {self._ruta}: {exc}") from None

    # ------------------------------------------------------------------
    def cerrar(self) -> None:
        with self._lock:
            self._con.close()

    def __del__(self) -> None:  # cierre de respaldo al liberar el objeto
        try:
            self._con.close()
        except Exception:  # noqa: BLE001 - nunca fallar en el destructor
            pass

    @property
    def ruta(self) -> Path:
        return self._ruta

    def _meta(self, clave: str) -> str | None:
        fila = self._con.execute("SELECT valor FROM meta WHERE clave = ?", (clave,)).fetchone()
        return fila["valor"] if fila else None

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------
    def cargar(self) -> dict[str, Any]:
        """Lee todo el estado en una sola lectura consistente.

        Devuelve ``{"contador": int, "slices": [dict], "imagenes": [dict],
        "tokens": {hash: dict}}`` y deja anotado el estado leído como base del
        siguiente ``guardar``.
        """
        with self._lock:
            try:
                self._con.execute("BEGIN")
                try:
                    contador = int(self._meta("contador") or 0)
                    filas_s = self._con.execute("SELECT id, datos FROM slices ORDER BY id").fetchall()
                    filas_i = self._con.execute("SELECT nombre, datos FROM imagenes").fetchall()
                    filas_t = self._con.execute("SELECT hash, datos FROM tokens").fetchall()
                finally:
                    self._con.execute("COMMIT")
                self._ultimo = {
                    "slices": {f["id"]: f["datos"] for f in filas_s},
                    "imagenes": {f["nombre"]: f["datos"] for f in filas_i},
                    "tokens": {f["hash"]: f["datos"] for f in filas_t},
                }
                return {
                    "contador": contador,
                    "slices": [json.loads(f["datos"]) for f in filas_s],
                    "imagenes": [json.loads(f["datos"]) for f in filas_i],
                    "tokens": {f["hash"]: json.loads(f["datos"]) for f in filas_t},
                }
            except (sqlite3.Error, ValueError) as exc:
                raise ErrorAlmacen(f"No se pudo leer la base de datos {self._ruta}: {exc}") from None

    def refrescar(self) -> dict[str, Any]:
        """Lee lo que OTROS procesos cambiaron desde la última lectura/escritura.

        Devuelve ``{"contador", "cambiados": [dict], "borrados": {ids},
        "imagenes": [dict], "tokens": {hash: dict}}``. ``cambiados`` incluye los
        slices nuevos o modificados; ``borrados`` los que ya no existen.
        """
        with self._lock:
            try:
                self._con.execute("BEGIN")
                try:
                    contador = int(self._meta("contador") or 0)
                    filas_s = self._con.execute("SELECT id, datos FROM slices").fetchall()
                    filas_i = self._con.execute("SELECT nombre, datos FROM imagenes").fetchall()
                    filas_t = self._con.execute("SELECT hash, datos FROM tokens").fetchall()
                finally:
                    self._con.execute("COMMIT")
                nuevo = {
                    "slices": {f["id"]: f["datos"] for f in filas_s},
                    "imagenes": {f["nombre"]: f["datos"] for f in filas_i},
                    "tokens": {f["hash"]: f["datos"] for f in filas_t},
                }
                previo = self._ultimo["slices"]
                cambiados = [json.loads(t) for sid, t in nuevo["slices"].items()
                             if previo.get(sid) != t]
                borrados = set(previo) - set(nuevo["slices"])
                self._ultimo = nuevo
                return {
                    "contador": contador,
                    "cambiados": cambiados,
                    "borrados": borrados,
                    "imagenes": [json.loads(t) for t in nuevo["imagenes"].values()],
                    "tokens": {h: json.loads(t) for h, t in nuevo["tokens"].items()},
                }
            except (sqlite3.Error, ValueError) as exc:
                raise ErrorAlmacen(f"No se pudo leer la base de datos {self._ruta}: {exc}") from None

    def esta_vacia(self) -> bool:
        with self._lock:
            n = self._con.execute("SELECT COUNT(*) AS n FROM slices").fetchone()["n"]
            return n == 0 and int(self._meta("contador") or 0) == 0

    # ------------------------------------------------------------------
    # Escritura
    # ------------------------------------------------------------------
    def siguiente_id(self) -> int:
        """Reserva el próximo número de slice de forma atómica entre procesos."""
        with self._lock:
            try:
                self._con.execute("BEGIN IMMEDIATE")
                try:
                    n = int(self._meta("contador") or 0) + 1
                    self._con.execute("UPDATE meta SET valor = ? WHERE clave = 'contador'", (str(n),))
                finally:
                    self._con.execute("COMMIT")
                return n
            except sqlite3.Error as exc:
                raise ErrorAlmacen(f"No se pudo reservar un ID de slice: {exc}") from None

    def guardar(self, contador: int, slices: list[dict], imagenes: list[dict],
                tokens: dict[str, dict]) -> None:
        """Aplica en UNA transacción lo que cambió desde la última lectura/escritura."""
        nuevo = {
            "slices": {d["id"]: _json(d) for d in slices},
            "imagenes": {d["nombre"]: _json(d) for d in imagenes},
            "tokens": {h: _json(t) for h, t in tokens.items()},
        }
        datos_s = {d["id"]: d for d in slices}
        with self._lock:
            try:
                self._con.execute("BEGIN IMMEDIATE")
                try:
                    ahora = time.time()
                    # --- slices (+ proyección nodos/enlaces) ---
                    for sid in set(self._ultimo["slices"]) - set(nuevo["slices"]):
                        self._con.execute("DELETE FROM slices WHERE id = ?", (sid,))
                    for sid, txt in nuevo["slices"].items():
                        if self._ultimo["slices"].get(sid) == txt:
                            continue
                        self._upsert_slice(datos_s[sid], txt, ahora)
                    # --- imágenes ---
                    for nom in set(self._ultimo["imagenes"]) - set(nuevo["imagenes"]):
                        self._con.execute("DELETE FROM imagenes WHERE nombre = ?", (nom,))
                    for nom, txt in nuevo["imagenes"].items():
                        if self._ultimo["imagenes"].get(nom) != txt:
                            self._con.execute(
                                "INSERT INTO imagenes(nombre, datos) VALUES(?, ?) "
                                "ON CONFLICT(nombre) DO UPDATE SET datos = excluded.datos", (nom, txt))
                    # --- tokens ---
                    for h in set(self._ultimo["tokens"]) - set(nuevo["tokens"]):
                        self._con.execute("DELETE FROM tokens WHERE hash = ?", (h,))
                    for h, txt in nuevo["tokens"].items():
                        if self._ultimo["tokens"].get(h) != txt:
                            self._con.execute(
                                "INSERT INTO tokens(hash, datos) VALUES(?, ?) "
                                "ON CONFLICT(hash) DO UPDATE SET datos = excluded.datos", (h, txt))
                    # El contador nunca retrocede (otro proceso pudo avanzarlo).
                    self._con.execute(
                        "UPDATE meta SET valor = CAST(MAX(CAST(valor AS INTEGER), ?) AS TEXT) "
                        "WHERE clave = 'contador'", (int(contador),))
                except BaseException:
                    self._con.execute("ROLLBACK")
                    raise
                self._con.execute("COMMIT")
                self._ultimo = nuevo
            except sqlite3.Error as exc:
                raise ErrorAlmacen(f"No se pudo guardar en la base de datos {self._ruta}: {exc}") from None

    def _upsert_slice(self, d: dict, texto: str, ahora: float) -> None:
        self._con.execute(
            "INSERT INTO slices(id, nombre, owner, estado, tipo_topologia, zona_disponibilidad, "
            "datos, actualizado) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET nombre=excluded.nombre, owner=excluded.owner, "
            "estado=excluded.estado, tipo_topologia=excluded.tipo_topologia, "
            "zona_disponibilidad=excluded.zona_disponibilidad, datos=excluded.datos, "
            "actualizado=excluded.actualizado",
            (d["id"], d["nombre"], d["owner"], d["estado"], d["tipo_topologia"],
             d.get("zona_disponibilidad"), texto, ahora))
        self._con.execute("DELETE FROM nodos WHERE slice_id = ?", (d["id"],))
        self._con.execute("DELETE FROM enlaces WHERE slice_id = ?", (d["id"],))
        for n in d.get("nodos", []):
            self._con.execute(
                "INSERT INTO nodos(slice_id, id, nombre, host_id, vcpu, ram_mb, disco_gb, imagen, "
                "flavor, estado, ip, acceso_internet) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (d["id"], n["id"], n["nombre"], n.get("host_id"), n["vcpu"], n["ram_mb"],
                 n["disco_gb"], n["imagen"], n.get("flavor"), n.get("estado"), n.get("ip"),
                 1 if n.get("acceso_internet") else 0))
        for e in d.get("enlaces", []):
            self._con.execute(
                "INSERT INTO enlaces(slice_id, id, nodo_origen, nodo_destino, nombre_red, vlan_id) "
                "VALUES(?,?,?,?,?,?)",
                (d["id"], e["id"], e["nodo_origen"], e["nodo_destino"], e.get("nombre_red"),
                 e.get("vlan_id")))

    # ------------------------------------------------------------------
    # Migración desde el JSON anterior
    # ------------------------------------------------------------------
    def migrar_desde_json(self, ruta_json: str | os.PathLike) -> int:
        """Importa un estado JSON previo a una base vacía. Devuelve cuántos slices pasó.

        No modifica ni borra el JSON original. Es idempotente: si la base ya fue
        migrada o ya tiene datos, no hace nada y devuelve 0.
        """
        ruta_json = Path(ruta_json)
        with self._lock:
            if self._meta("migrado_desde") is not None or not self.esta_vacia():
                return 0
            try:
                datos = json.loads(ruta_json.read_text(encoding="utf-8"))
                slices = list(datos["slices"])
                imagenes = list(datos.get("imagenes", []))
                tokens = dict(datos.get("tokens", {}))
                contador = int(datos["contador"])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ErrorAlmacen(f"No se pudo migrar {ruta_json}: {exc}") from None
            self._ultimo = {"slices": {}, "imagenes": {}, "tokens": {}}
            self.guardar(contador, slices, imagenes, tokens)
            self._con.execute("INSERT OR REPLACE INTO meta(clave, valor) VALUES('migrado_desde', ?)",
                              (str(ruta_json),))
            logger.info("Migrados %d slices desde %s a %s", len(slices), ruta_json, self._ruta)
            return len(slices)
