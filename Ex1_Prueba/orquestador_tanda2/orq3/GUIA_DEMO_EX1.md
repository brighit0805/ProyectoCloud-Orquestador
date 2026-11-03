# Guía del demo EX1 (se ejecuta en server4, dentro de este directorio)

Orden alineado con la secuencia de validación del profesor. Cada paso dice qué comando
correr y qué debe verse.

## 0. Antes del demo (una vez)
Las imágenes base (`cirros-0.6.2.qcow2`, `ubuntu-22.04.qcow2`) van SOLO en la carpeta de server4
`REPO_IMAGENES` (ver `config_cluster.py`). Los workers deben estar limpios (paso 2 de la validación):
```
python3 limpiar_workers.py                      # simulacro: muestra qué borraría
python3 limpiar_workers.py --confirmar          # deja workers sin VMs ni imágenes
python3 -m unittest discover -s tests           # 43 pruebas, todas OK
python3 verificar_cluster.py                    # repo de imágenes, bridges, restos
```

## 1. Despliegue (round robin)
```
python3 ejecutar_ex1_real.py --acceso
```
El orquestador copia cada imagen base desde server4 a cada worker que la necesita (lo informa),
y reintenta hasta 3 veces las operaciones que fallen. Debe listar VM1..VM6 con su worker: VM1,VM4 → worker1 · VM2,VM5 → worker2 · VM3,VM6 → worker3.

## 2. Procesos QEMU (processID por VM y worker)
```
python3 mostrar_vms.py
```

## 3. Discos QCOW2 con backing file
```
python3 mostrar_vms.py --disco
```
Cada disco debe decir `file format: qcow2` y `backing file: .../ubuntu-22.04.qcow2` (o cirros).

## 4. Aislamiento L2 (consola VNC o SSH a las VMs)
En VM1, VM3 y VM5 dejar corriendo `sudo tcpdump -i <interfaz> arp`.
Desde VM4: `sudo arping -I <interfaz> -c 3 10.30.3.2` (hacia VM1 por la VLAN 3003).
Solo debe verlo el vecino de esa VLAN (VM1); VM3 y VM5 no.
IPs de cada enlace: VM1 10.30.0.1/10.30.3.2 · VM2 10.30.0.2/10.30.1.1 · VM3 10.30.1.2/10.30.2.1 ·
VM4 10.30.2.2/10.30.3.1/10.30.4.1 · VM5 10.30.4.2/10.30.5.1 · VM6 10.30.5.2.

## 5. Internet y SSH (VM1 y VM3)
Modo por defecto (`MODO_ACCESO = "nat_libvirt"` en `config_cluster.py`): VM1 y VM3 llevan una 3.ª NIC
en la red NAT `default` de libvirt del worker, con DHCP; no hace falta root en el headnode.
(El modo `headnode` con `headnode_nat.sh` queda como alternativa.) La red `default` debe estar activa
en los workers: `virsh net-list`. Para SSH desde fuera, ver la IP DHCP de la VM con `virsh domifaddr`.
En VM1 y VM3: `ping -c3 8.8.8.8`. Desde afuera: `ssh -p 2201 ubuntu@10.20.11.142` (VM1) y `-p 2203` (VM3).
En VM2, VM4, VM5, VM6 el ping a 8.8.8.8 debe fallar.

## 6. Eliminación con borrado inteligente
```
python3 eliminar_ex1.py
python3 verificar_cluster.py     # no deben quedar VMs, discos demo-ex1-*, seed-* ni imágenes copiadas
```
Se borra cada VM y, en cada worker, la copia de la imagen base solo si la copió el orquestador y
ya nadie la usa.

## 7. Manejo de errores (reintento y rollback)
Modo simulado:
```
python3 demo_reintento.py                   # falla una operación, se reintenta y el slice queda activo
python3 demo_reintento.py --sin-reintentos  # el mismo fallo provoca rollback sin recursos huérfanos
```
