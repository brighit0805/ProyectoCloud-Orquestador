#!/bin/sh
# Configura en el HEADNODE la salida a Internet y el SSH entrante de VM1 y VM3.
# Debe ejecutarlo alguien con root (el usuario ubuntu del headnode no tiene sudo):
#     sudo sh headnode_nat.sh
# Es idempotente: cada regla se agrega solo si no existe.
set -e

EXT=ens3                    # interfaz hacia Internet
INT=ens4                    # interfaz hacia el clúster
RED_INTERNA=10.0.10.0/24
VM1=10.0.10.31              # IP de acceso de VM1  (debe coincidir con config_cluster.py)
VM3=10.0.10.33              # IP de acceso de VM3
PUERTO_VM1=2201             # SSH externo: <headnode>:2201 -> VM1:22
PUERTO_VM3=2203             # SSH externo: <headnode>:2203 -> VM3:22

# 1) Reenvío de paquetes (ahora y de forma persistente)
sysctl -w net.ipv4.ip_forward=1
grep -q '^net.ipv4.ip_forward=1' /etc/sysctl.conf || echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf

regla() {   # regla <tabla> <cadena> <args...>: agrega si no existe
  tabla=$1; cadena=$2; shift 2
  iptables -t "$tabla" -C "$cadena" "$@" 2>/dev/null || iptables -t "$tabla" -A "$cadena" "$@"
}

# 2) Salida a Internet: las VMs salen con la IP externa del headnode
regla nat POSTROUTING -s "$RED_INTERNA" -o "$EXT" -j MASQUERADE
regla filter FORWARD -i "$INT" -o "$EXT" -j ACCEPT
regla filter FORWARD -i "$EXT" -o "$INT" -m state --state ESTABLISHED,RELATED -j ACCEPT

# 3) SSH entrante hacia VM1 y VM3
regla nat PREROUTING -i "$EXT" -p tcp --dport "$PUERTO_VM1" -j DNAT --to-destination "$VM1:22"
regla nat PREROUTING -i "$EXT" -p tcp --dport "$PUERTO_VM3" -j DNAT --to-destination "$VM3:22"
regla filter FORWARD -i "$EXT" -o "$INT" -p tcp -d "$VM1" --dport 22 -j ACCEPT
regla filter FORWARD -i "$EXT" -o "$INT" -p tcp -d "$VM3" --dport 22 -j ACCEPT

echo "Listo. Reglas NAT actuales:"; iptables -t nat -S | grep -E 'MASQUERADE|DNAT'
