# Módulo de VM Placement — Lab 7 TEL141

## ¿Qué hace el módulo?

El módulo de VM Placement determina dónde debe colocarse una máquina virtual a partir de los recursos solicitados, la plataforma seleccionada y, cuando corresponde, la zona de disponibilidad.

Actúa antes del despliegue de la VM y reutiliza la lógica real del proyecto ubicada en `slice_orchestrator/vm_placement.py`, por lo que no implementa un algoritmo separado.

En Linux selecciona un host concreto según los recursos disponibles y la estrategia del Scheduler. En OpenStack genera las restricciones necesarias para que Nova realice la selección final del host dentro de la zona indicada.

## ¿Cómo se usa?

El módulo se ejecuta con:

`python3 vm-placement.py input-data.json`

El archivo `input-data.json` contiene los casos de prueba y requerimientos de las máquinas virtuales.

Cada caso genera como salida una estructura `PlacementDecision` con:

- host seleccionado;
- plataforma utilizada;
- argumentos adicionales de placement.

También puede utilizarse desde código mediante la función:

`decide_vm_placement()`

## Interfaz de entrada

La entrada utiliza la estructura `VMRequest`:

- `cpu`: número de vCPU requeridas.
- `ram`: memoria RAM requerida en MB.
- `disco`: almacenamiento requerido en GB.
- `plataforma`: `"linux"` u `"openstack"`.
- `zone`: zona de disponibilidad, cuando corresponda.
- `etiquetas`: metadatos opcionales para restricciones o políticas.
- `nombre`: identificador de la VM.

## Interfaz de salida

La salida utiliza la estructura `PlacementDecision`:

- `host`: host seleccionado en Linux; puede ser `None` en OpenStack.
- `plataforma`: plataforma utilizada.
- `args`: información adicional como estrategia, `scheduler_hints`, rechazo por falta de recursos o políticas asociadas.

## Plataformas soportadas

### Linux

El módulo evalúa los hosts disponibles considerando CPU, RAM y disco. Utiliza la lógica real del Scheduler del proyecto para seleccionar el host que cumpla con los requerimientos de la VM.

También puede considerar restricciones simplificadas mediante etiquetas, como hosts que deben evitarse.

### OpenStack

El módulo utiliza la zona de disponibilidad como restricción de placement y genera:

`availability_zone`

dentro de los `scheduler_hints`.

El orquestador define la zona y Nova selecciona el host final dentro de ella.

## Ejecución de pruebas

Los casos se encuentran en:

`input-data.json`

y se ejecutan mediante:

`python3 vm-placement.py input-data.json`

Los casos incluyen:

1. selección de host por CPU, RAM y disco;
2. selección de zona de disponibilidad en OpenStack;
3. rechazo por falta de recursos;
4. selección de plataforma Linux;
5. selección de plataforma OpenStack;
6. anti-afinidad simplificada mediante etiquetas.
