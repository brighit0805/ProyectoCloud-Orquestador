# Orquestador de slices — Grupo 2, TEL141

Implementa, mediante funciones Python y sin API HTTP, la creación y eliminación
de slices de máquinas virtuales sobre múltiples hosts Linux. Soporta las dos
topologías predefinidas (lineal y anillo) y la **EX1-Topology**, que es una
composición de ambas.

## Ejecución

```bash
python3 demo_ex1.py                          # las tres topologías + rollback
python3 demo.py                              # demo original del Entregable 3
python3 -m unittest discover -s tests -v     # 29 pruebas
```

El modo predeterminado es **simulado**: no toca ninguna infraestructura. La
presencia de `libvirt-python` no activa operaciones reales automáticamente.

Compatible con Python 3.8 o superior, incluida la instalación del servidor del
laboratorio.

## La EX1-Topology

Anillo `VM1–VM2–VM3–VM4–VM1` más rama lineal `VM4–VM5–VM6` injertada sobre VM4.

```python
from slice_orchestrator.topology_manager import construir_ex1_topology
from slice_orchestrator.orchestrator import desplegar_slice_existente
from slice_orchestrator.vm_placement import EstrategiaPlacement

slc = construir_ex1_topology("slice-ex1", "demo", "grupo2@pucp.edu.pe", "az-lab")
slc = desplegar_slice_existente(slc, workers, driver, allocator,
                                EstrategiaPlacement.ROUND_ROBIN)
```

| Característica | Valor |
|---|---|
| Nodos / enlaces | 6 / 6 |
| Flavor `ubuntu` | VM1, VM3, VM4, VM5 — 1 vCPU, 512 MB, 2.2 GB |
| Flavor `cirros` | VM2, VM6 — 1 vCPU, 512 MB, 1.0 GB |
| Salida a Internet y SSH entrante | VM1 y VM3 únicamente |
| Reparto sobre los workers | VM1·VM4 → worker1 · VM2·VM5 → worker2 · VM3·VM6 → worker3 |
| VLAN | una por enlace, del rango 3000–3999 |

Las topologías predefinidas se tratan como **generadores de grafo**, no como
plantillas cerradas: se parte de una del catálogo y se injertan otras sobre un
nodo existente, de modo que la EX1-Topology no es un caso especial escrito a
mano sino una composición que el usuario puede construir.

```python
construir_topologia(
    "s", "demo", "owner", TipoTopologia.ANILLO, 4,
    injertos=[Injerto(sobre="vm4", tipo=TipoTopologia.LINEAL, num_nodos=2)],
    flavor_map={"vm2": "cirros", "vm6": "cirros"},
    internet_access=["vm1", "vm3"],
)
```

Los nodos se referencian por clave corta (`"vm1"`, igual que en el diagrama),
por nombre completo o por id.

## Módulos

| Módulo | Responsabilidad |
|---|---|
| `models.py` | Entidades `Slice`, `Nodo`, `Enlace`, `Host`, `Flavor` y el catálogo de flavors |
| `topology_manager.py` | Genera nodos y enlaces, compone topologías con injertos y calcula el plan |
| `vm_placement.py` | Elige el host físico (estrategias balanceada y round robin) y administra capacidad |
| `network_manager.py` | Reserva y libera VLAN sin duplicarlas |
| `driver_linux.py` | Modo simulado explícito y modo real mediante libvirt/KVM |
| `orchestrator.py` | Coordina creación, rollback y eliminación |
| `demo_ex1.py` | Demostración de las tres topologías y del rollback |
| `tests/` | 29 pruebas unitarias |

## Novedades respecto al Entregable 3

- **Topología mixta por composición**: parámetro `injertos` en `construir_topologia`,
  más el atajo `construir_ex1_topology`.
- **Flavors**: dataclass `Flavor`, catálogo con `ubuntu` y `cirros`, y parámetro
  `flavor_map` para asignarlos por nodo.
- **Acceso a Internet selectivo**: campo `Nodo.acceso_internet` y parámetro
  `internet_access`.
- **Reparto round robin**: `EstrategiaPlacement.ROUND_ROBIN`, que es el que
  espera el procedimiento de validación de la EX1.
- **Plan por olas**: `calcular_plan_despliegue` devuelve además `olas`, la
  agrupación de operaciones que pueden lanzarse en paralelo.
- **Estado BORRADOR**: permite diseñar un slice y desplegarlo más tarde con
  `desplegar_slice_existente`.
- **Corrección**: `orden_rollback` eliminaba las redes antes que las VMs. Ahora
  es el inverso exacto del orden de creación, con las VMs primero.

## Pendiente para el despliegue real

1. URI y acceso SSH/libvirt de cada worker en `inventario_laboratorio()`.
2. Bridge OVS provider y enlace troncal entre workers y el switch OFS.
3. Rango VLAN confirmado para el Grupo 2.
4. Pool de almacenamiento e imágenes base `ubuntu-22.04.qcow2` y `cirros-0.6.2.qcow2`.
5. NAT en el headnode para la salida a Internet de VM1 y VM3, y redirección de
   puerto para el SSH entrante.
6. Proxy VNC con tokens para el acceso a consola.
7. Recolector de discos derivados sin uso.

Persistencia, cola de tareas, autenticación, monitoreo, API REST y OpenStack
quedan para iteraciones posteriores, según el plan de la arquitectura.

## Driver OpenStack (R3) — en construcción

`slice_orchestrator/driver_openstack.py` despliega los slices en OpenStack usando
solo sus APIs REST (Keystone, Neutron y Nova), con el flujo del Lab 6. Tiene los
mismos métodos que `DriverLinux` (contrato en `driver_base.py`), así que
`desplegar_slice_existente` y `eliminar_slice` funcionan con cualquiera de los dos.

```bash
python3 demo_openstack.py                        # despliega y elimina; imprime cada petición REST
python3 demo_openstack.py --fallo                # falla una operación -> rollback, 0 recursos huérfanos
python3 demo_openstack.py --fallo --reintentos 2 # la misma falla se recupera
python3 -m unittest discover -s tests -t .       # 67 pruebas (21 del driver OpenStack)
```

| Operación del orquestador | Petición a OpenStack |
|---|---|
| `preparar_slice` | Keystone: `POST /projects`, `PUT /projects/{p}/users/{u}/roles/{r}` |
| `crear_red` | Neutron: `POST /networks` y `POST /subnets` (una por enlace) |
| `crear_vm` | Neutron: `POST /ports` (MAC e IP de `direccionamiento`); Nova: `POST /servers` |
| `eliminar_vm` | Nova: `DELETE /servers/{id}`; Neutron: `DELETE /ports/{id}` |
| `eliminar_red` / `liberar_slice` | Neutron: `DELETE` subred y red; Keystone: `DELETE /projects/{id}` |
| `descubrir_hosts` | Nova: `/os-hypervisors/detail` y `/os-availability-zone/detail` |

**Estado de validación:** probado en modo simulado (OpenStack en memoria). Todavía
NO se ha probado contra el clúster real. Para hacerlo, copie `.env.example` a `.env`,
abra los túneles SSH del Lab 6, use `ModoOpenStack.REAL` y un proyecto de prueba.
La VLAN real de cada enlace la asigna Neutron; el VLAN lógico del orquestador no se fuerza.

## Interfaz de usuario de consola (R1B)

CLI para gestionar el ciclo de vida de slices, sobre una capa de servicio
multiusuario (`servicio_slices.py`) que es el precursor mínimo de R1C. Backend:
driver Linux **simulado**; no toca ningún clúster.

```bash
python3 demo_r1b.py                                   # recorre los 8 criterios de R1B
python3 -m slice_orchestrator.cli --help
python3 -m slice_orchestrator.cli -u ana topologias
python3 -m slice_orchestrator.cli -u ana slice crear --nombre ex1 --tipo anillo --nodos 4 \
    --rama vm4:lineal:2 --flavor vm2=cirros --flavor vm6=cirros --internet vm1 --internet vm3 \
    --zona az-lab --desplegar --estrategia round_robin
python3 -m slice_orchestrator.cli -u ana slice editar slice-001 --agregar-enlace vm2,vm5
python3 -m slice_orchestrator.cli -u ana slice exportar slice-001 plantilla.json
python3 -m slice_orchestrator.cli -u bob slice importar plantilla.json --nombre copia
python3 -m slice_orchestrator.cli -u ana recursos
python3 -m slice_orchestrator.cli -u ana consola emitir slice-001 vm3
```

| Criterio R1B (rúbrica) | Pts | Dónde |
|---|---|---|
| Concurrencia de usuarios | 1 | `ServicioSlices` (cerrojo; aislamiento por usuario) |
| Editar, listar y eliminar slices | 0,5 | `editar_slice`, `listar_slices`, `eliminar_slice` |
| Topologías predefinidas con capacidad por VM | 0,5 | `listar_topologias`, `listar_flavors`, `capacidad` |
| Componer y editar slices arbitrarias | 2 | `composicion.py` (injertar, nodos, enlaces, validación) |
| Exportar / importar plantillas | 0,5 | `exportar_plantilla`, `crear_desde_plantilla` |
| Consumo de recursos | 0,5 | `consumo_recursos` |
| Importar imágenes | 0,5 | `importar_imagen` |
| Tokens de consola | 0,5 | `emitir_token_consola`, `canjear_token_consola` |

Limitaciones conocidas (ver `LEEME_R1B.txt`): sin autenticación real, sin API
HTTP, sin jobs, el despliegue se serializa, los slices activos no se editan y el
bus no está implementado.
