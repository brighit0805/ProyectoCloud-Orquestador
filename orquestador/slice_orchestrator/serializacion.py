"""
serializacion.py
----------------
Conversión del modelo (``Slice``) a diccionarios JSON y de vuelta, más las
plantillas de R1B (HLD §3.6).

Dos formatos distintos, con propósitos distintos:

* **Estado** (``slice_a_dict`` / ``slice_desde_dict``): el slice completo, con
  VLAN, host e IP. Lo usa el servicio para persistir. Es de confianza porque lo
  escribe el propio sistema.
* **Plantilla** (``plantilla_de`` / ``slice_desde_plantilla``): solo el *diseño*.
  Se comparte entre usuarios, así que al importarla se valida todo como entrada
  no confiable. NO incluye VLAN, host, IP, propietario ni fechas: pertenecen al
  despliegue concreto, no al diseño.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from . import composicion
from .models import (
    CATALOGO_FLAVORS,
    Enlace,
    EstadoSlice,
    Nodo,
    Slice,
    TipoTopologia,
)

VERSION_PLANTILLA = "1.0"
_CLAVE_NODO = re.compile(r"^vm[0-9]{1,3}$")


# ---------------------------------------------------------------------------
# Estado completo
# ---------------------------------------------------------------------------
def slice_a_dict(slc: Slice) -> dict[str, Any]:
    return {
        "id": slc.id,
        "nombre": slc.nombre,
        "owner": slc.owner,
        "tipo_topologia": slc.tipo_topologia.value,
        "estado": slc.estado.value,
        "zona_disponibilidad": slc.zona_disponibilidad,
        "nodos": [asdict(n) for n in slc.nodos],
        "enlaces": [asdict(e) for e in slc.enlaces],
    }


def slice_desde_dict(d: dict[str, Any]) -> Slice:
    return Slice(
        id=d["id"],
        nombre=d["nombre"],
        owner=d["owner"],
        tipo_topologia=TipoTopologia(d["tipo_topologia"]),
        estado=EstadoSlice(d["estado"]),
        zona_disponibilidad=d.get("zona_disponibilidad"),
        nodos=[Nodo(**n) for n in d["nodos"]],
        enlaces=[Enlace(**e) for e in d["enlaces"]],
    )


# ---------------------------------------------------------------------------
# Plantillas
# ---------------------------------------------------------------------------
def plantilla_de(slc: Slice) -> dict[str, Any]:
    """Serializa el diseño del slice: topología, capacidad e imagen de cada nodo."""
    claves = {n.id: composicion.clave_de(n) for n in slc.nodos}
    return {
        "version": VERSION_PLANTILLA,
        "nombre_sugerido": slc.nombre,
        "tipo_topologia": slc.tipo_topologia.value,      # informativo
        "zona_disponibilidad": slc.zona_disponibilidad,
        "nodos": [
            {
                "clave": claves[n.id],
                "flavor": n.flavor,
                "vcpu": n.vcpu,
                "ram_mb": n.ram_mb,
                "disco_gb": n.disco_gb,
                "imagen": n.imagen,
                "acceso_internet": n.acceso_internet,
            }
            for n in slc.nodos
        ],
        "enlaces": [[claves[e.nodo_origen], claves[e.nodo_destino]] for e in slc.enlaces],
    }


def _exigir(condicion: bool, mensaje: str) -> None:
    if not condicion:
        raise ValueError(f"Plantilla inválida: {mensaje}")


def slice_desde_plantilla(plantilla: Any, slice_id: str, nombre: str, owner: str,
                          imagenes_validas: set[str],
                          zonas_validas: set[str] | None = None) -> Slice:
    """Crea un slice BORRADOR a partir de una plantilla, validándola entera.

    ``imagenes_validas`` es el catálogo de imágenes vigente: una plantilla que
    referencia una imagen que este sistema no tiene se rechaza, en vez de fallar
    más tarde al desplegar. Lanza ValueError con un mensaje claro ante cualquier
    problema; no devuelve nada a medias.
    """
    _exigir(isinstance(plantilla, dict), "debe ser un objeto JSON")
    _exigir(plantilla.get("version") == VERSION_PLANTILLA,
            f"versión no soportada (se espera '{VERSION_PLANTILLA}')")
    nodos_json = plantilla.get("nodos")
    enlaces_json = plantilla.get("enlaces")
    _exigir(isinstance(nodos_json, list) and nodos_json, "falta la lista 'nodos'")
    _exigir(isinstance(enlaces_json, list), "falta la lista 'enlaces'")
    _exigir(len(nodos_json) <= composicion.MAX_NODOS,
            f"tiene más de {composicion.MAX_NODOS} nodos")

    zona = plantilla.get("zona_disponibilidad")
    _exigir(zona is None or isinstance(zona, str), "'zona_disponibilidad' debe ser texto")
    if zona is not None and zonas_validas is not None and zona not in zonas_validas:
        zona = None      # la zona es del despliegue de origen; si no existe aquí, se elige otra

    slc = Slice(id=slice_id, nombre=nombre, owner=owner,
                tipo_topologia=TipoTopologia.ARBITRARIA, estado=EstadoSlice.BORRADOR,
                zona_disponibilidad=zona)
    por_clave: dict[str, Nodo] = {}
    for i, nj in enumerate(nodos_json):
        _exigir(isinstance(nj, dict), f"el nodo {i} debe ser un objeto")
        clave = nj.get("clave")
        _exigir(isinstance(clave, str) and _CLAVE_NODO.match(clave) is not None,
                f"clave de nodo inválida en la posición {i}")
        _exigir(clave not in por_clave, f"clave de nodo repetida: {clave}")
        flavor = nj.get("flavor")
        _exigir(flavor is None or flavor in CATALOGO_FLAVORS or flavor == composicion.FLAVOR_PERSONALIZADO,
                f"flavor desconocido en {clave}")
        acceso = nj.get("acceso_internet", False)
        _exigir(isinstance(acceso, bool), f"'acceso_internet' de {clave} debe ser verdadero o falso")
        imagen = nj.get("imagen")
        _exigir(isinstance(imagen, str) and imagen in imagenes_validas,
                f"la imagen de {clave} no está en el catálogo de este sistema")

        nodo = Nodo.desde_flavor(
            id=f"{slice_id}-n{i}", nombre=f"{nombre}-{clave}",
            flavor=flavor if flavor in CATALOGO_FLAVORS else "ubuntu",
            acceso_internet=acceso)
        capacidad: dict[str, Any] = {"imagen": imagen}
        for campo, tipos in (("vcpu", int), ("ram_mb", int), ("disco_gb", (int, float))):
            valor = nj.get(campo)
            _exigir(isinstance(valor, tipos) and not isinstance(valor, bool),
                    f"'{campo}' de {clave} debe ser un número")
            capacidad[campo] = valor
        try:
            composicion.aplicar_capacidad(nodo, **capacidad)
        except ValueError as exc:
            raise ValueError(f"Plantilla inválida: {clave}: {exc}") from None
        # conserva el nombre de flavor original si coincide exactamente con el catálogo
        if flavor in CATALOGO_FLAVORS:
            f = CATALOGO_FLAVORS[flavor]
            if (nodo.vcpu, nodo.ram_mb, nodo.disco_gb, nodo.imagen) == (
                    f.vcpu, f.ram_mb, f.disco_gb, f.imagen):
                nodo.flavor = f.nombre
        por_clave[clave] = nodo
        slc.nodos.append(nodo)

    for j, par in enumerate(enlaces_json):
        _exigir(isinstance(par, list) and len(par) == 2 and all(isinstance(x, str) for x in par),
                f"el enlace {j} debe ser un par de claves de nodo")
        _exigir(par[0] in por_clave and par[1] in por_clave,
                f"el enlace {j} nombra un nodo que no existe")
        try:
            composicion.agregar_enlace(slc, par[0], par[1])
        except ValueError as exc:
            raise ValueError(f"Plantilla inválida: enlace {j}: {exc}") from None

    slc.tipo_topologia = composicion.clasificar(slc)
    problemas = composicion.validar(slc)
    _exigir(not problemas, "; ".join(problemas))
    return slc
