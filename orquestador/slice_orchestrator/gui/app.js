"use strict";
/* app.js — GUI del Orquestador de Slices (Grupo 2, 23-nov-2026)
 *
 * Habla con la API HTTP de R1C (api_http.py) en el mismo origen. Contrato
 * verificado contra el servicio_slices.py real del headnode: crear_slice/
 * _construir, ver_slice, listar_zonas, listar_topologias, consumo_recursos,
 * exportar_plantilla/crear_desde_plantilla, y las fórmulas de aristas de
 * topology_manager._figura_a_pares (replicadas en construirAristasBase).
 */

// ------------------------------------------------------------------
// Estado global
// ------------------------------------------------------------------
const estado = {
  token: null, usuario: null, rol: null,
  catalogo: [], zonas: [], slices: [], verTodos: false,
  trabajos: [],
  filtroSlices: "", detalle: null, nodoDetalle: null,
};

// Escapa texto del servidor antes de meterlo en innerHTML.
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const diseno = {
  enlacesQuitados: new Set(),
  enlacesExtra: new Set(),
  injertos: [],
  capacidad: [],
  internet: new Set(),
};

let valorPrevioTipo = null, valorPrevioNodos = null, valorPrevioRamif = null;
let enviandoSlice = false;

try {
  const guardado = sessionStorage.getItem("orq_sesion");
  if (guardado) Object.assign(estado, JSON.parse(guardado));
} catch (_) { /* sin almacenamiento disponible: se sigue sin sesión recordada */ }

function guardarSesion() {
  try {
    sessionStorage.setItem("orq_sesion", JSON.stringify({ token: estado.token, usuario: estado.usuario, rol: estado.rol }));
  } catch (_) { /* no crítico */ }
}

// ------------------------------------------------------------------
// Llamadas a la API
// ------------------------------------------------------------------
async function api(metodo, ruta, cuerpo) {
  const opciones = { method: metodo, headers: {} };
  if (estado.token) opciones.headers["Authorization"] = "Bearer " + estado.token;
  if (cuerpo !== undefined) {
    opciones.headers["Content-Type"] = "application/json";
    opciones.body = JSON.stringify(cuerpo);
  }
  const resp = await fetch(ruta, opciones);
  let datos = null;
  try { datos = await resp.json(); } catch (_) { /* respuesta vacía */ }
  if (!resp.ok) throw new Error((datos && datos.error) ? datos.error : `Error HTTP ${resp.status}`);
  return datos;
}

// ------------------------------------------------------------------
// Arranque
// ------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("form-login").addEventListener("submit", onLogin);
  document.getElementById("btn-salir").addEventListener("click", onSalir);
  document.querySelectorAll(".tab").forEach(b => b.addEventListener("click", () => activarTab(b.dataset.tab)));
  configurarBarraYMenu();
  document.getElementById("btn-nuevo-slice").addEventListener("click", () => activarTab("disenar"));
  document.getElementById("buscar-slices").addEventListener("input", e => { estado.filtroSlices = e.target.value.trim().toLowerCase(); renderTablaSlices(); });
  document.getElementById("det-volver").addEventListener("click", () => activarTab("slices"));
  document.getElementById("det-refrescar").addEventListener("click", () => estado.detalle && verSlice(estado.detalle.id));
  document.getElementById("det-exportar").addEventListener("click", () => estado.detalle && exportarPlantilla(estado.detalle.id, estado.detalle.nombre));
  document.getElementById("det-desplegar").addEventListener("click", () => estado.detalle && desplegarSlice(estado.detalle.id, estado.detalle.nombre));
  document.getElementById("det-eliminar").addEventListener("click", () => estado.detalle && eliminarSlice(estado.detalle.id, estado.detalle.nombre));
  document.querySelectorAll(".btn-redondo[data-objetivo]").forEach(b => b.addEventListener("click", () => {
    const id = b.dataset.objetivo;
    if (b.dataset.zoom === "in") aplicarZoom(id, 1.25);
    else if (b.dataset.zoom === "out") aplicarZoom(id, 0.8);
    else if (b.dataset.zoom === "fit") ajustarVista(id);
    else if (b.dataset.pan) moverVista(id, b.dataset.pan);
  }));
  configurarArrastreYRueda("det-lienzo");
  document.getElementById("toggle-todos-trabajos").addEventListener("change", e => {
    estado.verTodos = e.target.checked;
    document.getElementById("toggle-todos").checked = e.target.checked;
    cargarSlices(); cargarTrabajos();
  });
  document.getElementById("form-usuario").addEventListener("submit", onCrearUsuario);
  document.getElementById("btn-refrescar-usuarios").addEventListener("click", cargarUsuarios);
  document.getElementById("btn-revisar-residuos").addEventListener("click", () => revisarResiduos(false));
  document.getElementById("btn-limpiar-residuos").addEventListener("click", () => revisarResiduos(true));
  document.getElementById("toggle-todos").addEventListener("change", e => {
    estado.verTodos = e.target.checked;
    document.getElementById("toggle-todos-trabajos").checked = e.target.checked;
    cargarSlices(); cargarTrabajos();
  });

  document.getElementById("d-tipo").addEventListener("change", () => onCambioTopologiaBase("tipo"));
  document.getElementById("d-nodos").addEventListener("change", () => onCambioTopologiaBase("nodos"));
  document.getElementById("d-ramificacion").addEventListener("change", () => onCambioTopologiaBase("ramif"));
  document.getElementById("d-nombre").addEventListener("input", () => limpiarError("d-nombre"));

  document.getElementById("inj-tipo").addEventListener("change", () => {
    document.getElementById("inj-ramificacion").classList.toggle("oculto", document.getElementById("inj-tipo").value !== "arbol");
  });
  document.getElementById("inj-agregar").addEventListener("click", agregarInjerto);
  document.getElementById("cap-agregar").addEventListener("click", agregarCapacidad);
  document.getElementById("net-agregar").addEventListener("click", agregarInternet);
  document.getElementById("d-crear").addEventListener("click", onCrearSlice);
  document.getElementById("plant-crear").addEventListener("click", onCrearDesdePlantilla);

  document.querySelectorAll(".modo-tab").forEach(b => b.addEventListener("click", () => activarModo(b.dataset.modo)));
  document.querySelectorAll(".btn-zoom").forEach(b => b.addEventListener("click", () => {
    const svg = canvasVisible();
    if (b.dataset.zoom === "in") aplicarZoom(svg.id, 1.25);
    else if (b.dataset.zoom === "out") aplicarZoom(svg.id, 0.8);
    else ajustarVista(svg.id);
  }));
  configurarArrastreYRueda("d-lienzo");
  configurarArrastreYRueda("d-preview");

  document.getElementById("btn-refrescar-slices").addEventListener("click", cargarSlices);
  document.getElementById("btn-refrescar-recursos").addEventListener("click", cargarRecursos);
  document.getElementById("btn-refrescar-trabajos").addEventListener("click", cargarTrabajos);
  document.getElementById("btn-refrescar-red").addEventListener("click", cargarBroadcast);

  if (estado.token) mostrarApp();
});

// ------------------------------------------------------------------
// Login / sesión
// ------------------------------------------------------------------
async function onLogin(ev) {
  ev.preventDefault();
  const usuario = document.getElementById("login-usuario").value.trim();
  const contrasena = document.getElementById("login-password").value;
  const err = document.getElementById("login-error");
  err.classList.add("oculto");
  try {
    const datos = await api("POST", "/login", { usuario, contrasena });
    estado.token = datos.token; estado.rol = datos.rol; estado.usuario = usuario;
    guardarSesion();
    mostrarApp();
  } catch (e) {
    err.textContent = e.message;
    err.classList.remove("oculto");
  }
}

function onSalir() {
  detenerSondeo();
  cerrarMenuUsuario();
  estado.token = null; estado.usuario = null; estado.rol = null;
  try { sessionStorage.removeItem("orq_sesion"); } catch (_) {}
  document.getElementById("vista-app").classList.add("oculto");
  document.getElementById("vista-login").classList.remove("oculto");
  document.getElementById("login-password").value = "";
}

async function mostrarApp() {
  document.getElementById("vista-login").classList.add("oculto");
  document.getElementById("vista-app").classList.remove("oculto");
  document.getElementById("sesion-usuario").textContent = estado.usuario;
  document.getElementById("sesion-rol").textContent = estado.rol;
  document.getElementById("menu-nombre").textContent = estado.usuario;
  document.getElementById("menu-rol").textContent = estado.rol;
  const inicial = (estado.usuario || "?").trim().charAt(0);
  for (const id of ["avatar-barra", "avatar-menu", "avatar-lateral"]) document.getElementById(id).textContent = inicial;
  document.getElementById("toggle-todos-wrap").classList.toggle("oculto", estado.rol !== "admin");
  document.body.classList.toggle("es-admin", estado.rol === "admin");

  try {
    const [catalogo, zonas] = await Promise.all([api("GET", "/catalogo"), api("GET", "/zonas")]);
    estado.catalogo = catalogo; estado.zonas = zonas;
  } catch (e) {
    if (/401|token|credencial/i.test(e.message)) { onSalir(); return; }
  }
  poblarCatalogoYZonas();
  activarTab("slices");
  cargarSlices();
  cargarRecursos();
  iniciarSondeo();
}

function poblarCatalogoYZonas() {
  const selTipo = document.getElementById("d-tipo");
  const selInjTipo = document.getElementById("inj-tipo");
  selTipo.innerHTML = ""; selInjTipo.innerHTML = "";
  for (const t of estado.catalogo) {
    selTipo.appendChild(new Option(`${t.tipo} (${t.enlaces})`, t.tipo));
    if (t.tipo !== "bus") selInjTipo.appendChild(new Option(t.tipo, t.tipo));
  }
  const selZona = document.getElementById("d-zona");
  selZona.innerHTML = '<option value="">Sin especificar</option>';
  for (const z of estado.zonas) selZona.appendChild(new Option(`${z.zona} (${z.hosts.join(", ")})`, z.zona));
  aplicarCambioTopologia();
  valorPrevioTipo = document.getElementById("d-tipo").value;
  valorPrevioNodos = document.getElementById("d-nodos").value;
  valorPrevioRamif = document.getElementById("d-ramificacion").value;
}

// ------------------------------------------------------------------
// Tabs y modos
// ------------------------------------------------------------------
const TITULOS = {
  slices: ["Slices", "Información general sobre los slices existentes"],
  detalle: ["Detalle del slice", "Topología del slice: en qué servidor quedó cada VM y la VLAN de cada enlace"],
  disenar: ["Diseñar slice", "Arma la topología, elige capacidad y acceso a Internet, y créalo"],
  trabajos: ["Trabajos", "Cola de despliegues y eliminaciones (multiusuario)"],
  recursos: ["Recursos", "Consumo de los servidores del clúster y de tus slices"],
  red: ["Red", "Control de broadcast por servidor (R5)"],
  permisos: ["Roles y permisos", "Qué puede hacer un usuario y qué puede hacer un administrador"],
  usuarios: ["Usuarios", "Administración de cuentas y mantenimiento del clúster (solo administrador)"],
};

function activarTab(nombre) {
  const menu = nombre === "detalle" ? "slices" : nombre;
  document.querySelectorAll(".tab").forEach(b => {
    const activo = b.dataset.tab === menu;
    b.classList.toggle("activo", activo); b.setAttribute("aria-selected", String(activo));
  });
  document.querySelectorAll(".panel-tab").forEach(p => p.classList.toggle("oculto", p.id !== "tab-" + nombre));
  const [titulo, sub] = TITULOS[nombre] || ["", ""];
  document.getElementById("titulo-pagina").textContent = titulo;
  document.getElementById("subtitulo-pagina").textContent = sub;
  document.body.classList.remove("menu-abierto");
  if (nombre === "slices") cargarSlices();
  if (nombre === "trabajos") cargarTrabajos();
  if (nombre === "recursos") cargarRecursos();
  if (nombre === "red") cargarBroadcast();
  if (nombre === "permisos") cargarPermisos();
  if (nombre === "usuarios") cargarUsuarios();
}

function activarModo(modo) {
  document.querySelectorAll(".modo-tab").forEach(b => {
    const activo = b.dataset.modo === modo;
    b.classList.toggle("activo", activo); b.setAttribute("aria-selected", String(activo));
  });
  document.getElementById("d-lienzo").classList.toggle("oculto", modo !== "editar");
  document.getElementById("d-preview").classList.toggle("oculto", modo !== "preview");
  document.getElementById("ayuda-editar").classList.toggle("oculto", modo !== "editar");
  document.getElementById("ayuda-preview").classList.toggle("oculto", modo !== "preview");
  if (modo === "preview") dibujarPreview();
}

function canvasVisible() {
  const l = document.getElementById("d-lienzo");
  return l.classList.contains("oculto") ? document.getElementById("d-preview") : l;
}

// ------------------------------------------------------------------
// Zoom / paneo del lienzo
// ------------------------------------------------------------------
const visores = {
  "d-lienzo": { escala: 1, x: 0, y: 0 },
  "d-preview": { escala: 1, x: 0, y: 0 },
  "det-lienzo": { escala: 1, x: 0, y: 0 },
};

function moverVista(svgId, dir) {
  const v = visores[svgId], paso = 80;
  if (dir === "arriba") v.y += paso; else if (dir === "abajo") v.y -= paso;
  else if (dir === "izquierda") v.x += paso; else if (dir === "derecha") v.x -= paso;
  aplicarTransform(svgId);
}

function aplicarTransform(svgId) {
  const v = visores[svgId];
  const g = document.querySelector(`#${svgId} .capa-lienzo`);
  if (g) g.setAttribute("transform", `translate(${v.x},${v.y}) scale(${v.escala})`);
}

function aplicarZoom(svgId, factor, cx, cy) {
  const v = visores[svgId];
  const nueva = Math.min(3, Math.max(0.4, v.escala * factor));
  const f = nueva / v.escala;
  if (cx !== undefined) { v.x = cx - (cx - v.x) * f; v.y = cy - (cy - v.y) * f; }
  v.escala = nueva;
  aplicarTransform(svgId);
}

function ajustarVista(svgId) {
  const svg = document.getElementById(svgId);
  const g = svg.querySelector(".capa-lienzo");
  visores[svgId] = { escala: 1, x: 0, y: 0 };
  aplicarTransform(svgId);
  let caja;
  try { caja = g.getBBox(); } catch (_) { return; }
  if (!caja || caja.width === 0) return;
  const vb = svg.viewBox.baseVal;
  const pad = 40;
  const escala = Math.min((vb.width - pad * 2) / caja.width, (vb.height - pad * 2) / caja.height, 2);
  const x = vb.width / 2 - (caja.x + caja.width / 2) * escala;
  const y = vb.height / 2 - (caja.y + caja.height / 2) * escala;
  visores[svgId] = { escala, x, y };
  aplicarTransform(svgId);
}

function configurarArrastreYRueda(svgId) {
  const svg = document.getElementById(svgId);
  let arrastrando = false, ox = 0, oy = 0;
  svg.addEventListener("pointerdown", ev => {
    if (ev.target.closest(".nodo-grupo, .vm-grupo")) return;
    arrastrando = true; ox = ev.clientX; oy = ev.clientY;
    svg.classList.add("agarrando"); svg.setPointerCapture(ev.pointerId);
  });
  svg.addEventListener("pointermove", ev => {
    if (!arrastrando) return;
    const v = visores[svgId];
    const escalaPantalla = svg.clientWidth ? svg.viewBox.baseVal.width / svg.clientWidth : 1;
    v.x += (ev.clientX - ox) * escalaPantalla; v.y += (ev.clientY - oy) * escalaPantalla;
    ox = ev.clientX; oy = ev.clientY;
    aplicarTransform(svgId);
  });
  const soltar = () => { arrastrando = false; svg.classList.remove("agarrando"); };
  svg.addEventListener("pointerup", soltar);
  svg.addEventListener("pointerleave", soltar);
  svg.addEventListener("wheel", ev => {
    ev.preventDefault();
    aplicarZoom(svgId, ev.deltaY < 0 ? 1.1 : 0.9);
  }, { passive: false });
}

// ------------------------------------------------------------------
// Validación
// ------------------------------------------------------------------
function mostrarError(campo, texto) {
  const input = document.getElementById(campo);
  const err = document.getElementById(campo + "-error");
  input.setAttribute("aria-invalid", "true");
  if (err) { err.textContent = texto; err.classList.remove("oculto"); }
}
function limpiarError(campo) {
  const input = document.getElementById(campo);
  const err = document.getElementById(campo + "-error");
  input.removeAttribute("aria-invalid");
  if (err) { err.textContent = ""; err.classList.add("oculto"); }
}

function validarFormulario() {
  limpiarError("d-nombre"); limpiarError("d-nodos");
  let primerError = null;
  const nombre = document.getElementById("d-nombre").value.trim();
  if (!/^[a-z0-9][a-z0-9-]{0,29}$/.test(nombre)) {
    mostrarError("d-nombre", "Usa minúsculas, números y guiones (1 a 30 caracteres).");
    primerError = primerError || "d-nombre";
  }
  const tipo = document.getElementById("d-tipo").value;
  const info = estado.catalogo.find(t => t.tipo === tipo);
  const nodos = parseInt(document.getElementById("d-nodos").value, 10);
  if (!Number.isInteger(nodos) || (info && nodos < info.minimo_nodos)) {
    mostrarError("d-nodos", info ? `Mínimo ${info.minimo_nodos} nodos para ${tipo}.` : "Ingresa un número entero de nodos.");
    primerError = primerError || "d-nodos";
  }
  if (primerError) { document.getElementById(primerError).focus(); return false; }
  return true;
}

// ------------------------------------------------------------------
// Diseñador — topología
// ------------------------------------------------------------------
function paresPorIndice(tipo, n, ramificacion) {
  const pares = [];
  if (tipo === "lineal" || tipo === "bus") { for (let i = 0; i < n - 1; i++) pares.push([i, i + 1]); }
  else if (tipo === "anillo") { for (let i = 0; i < n; i++) pares.push([i, (i + 1) % n]); }
  else if (tipo === "malla") { for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) pares.push([i, j]); }
  else if (tipo === "arbol") {
    const r = Math.max(2, ramificacion || 2);
    for (let i = 1; i < n; i++) pares.push([Math.floor((i - 1) / r), i]);
  }
  return pares;
}
function construirAristasBase(tipo, n, ramificacion) {
  const ids = Array.from({ length: n }, (_, i) => `vm${i + 1}`);
  return paresPorIndice(tipo, n, ramificacion).map(([i, j]) => [ids[i], ids[j]]);
}
function construirAristasSobreIds(tipo, ids, ramificacion) {
  return paresPorIndice(tipo, ids.length, ramificacion).map(([i, j]) => [ids[i], ids[j]]);
}
function claveArista(a, b) { return a < b ? `${a}|${b}` : `${b}|${a}`; }

function onCambioTopologiaBase(origen) {
  const tipoEl = document.getElementById("d-tipo"), nodosEl = document.getElementById("d-nodos"), ramifEl = document.getElementById("d-ramificacion");
  const hayEdiciones = diseno.enlacesQuitados.size > 0 || diseno.enlacesExtra.size > 0 || diseno.injertos.length > 0;
  if (hayEdiciones) {
    const ok = confirm("Cambiar la topología reiniciará los enlaces manuales y los injertos que configuraste. ¿Continuar?");
    if (!ok) {
      if (origen === "tipo") tipoEl.value = valorPrevioTipo;
      else if (origen === "nodos") nodosEl.value = valorPrevioNodos;
      else ramifEl.value = valorPrevioRamif;
      return;
    }
  }
  aplicarCambioTopologia();
  valorPrevioTipo = tipoEl.value; valorPrevioNodos = nodosEl.value; valorPrevioRamif = ramifEl.value;
}

function aplicarCambioTopologia() {
  limpiarError("d-nodos");
  const tipo = document.getElementById("d-tipo").value;
  const info = estado.catalogo.find(t => t.tipo === tipo);
  document.getElementById("wrap-ramificacion").classList.toggle("oculto", tipo !== "arbol");
  document.getElementById("d-nodos-ayuda").textContent = info ? `Mínimo ${info.minimo_nodos} nodos (${info.restriccion}).` : "";
  diseno.enlacesQuitados.clear(); diseno.enlacesExtra.clear(); diseno.injertos = [];
  renderInjertos(); poblarSelectorSobre();
  redibujarLienzo();
}

function nodosBaseActuales() {
  const n = parseInt(document.getElementById("d-nodos").value, 10) || 0;
  return Array.from({ length: n }, (_, i) => `vm${i + 1}`);
}
function todasLasClaves() {
  const claves = nodosBaseActuales();
  let siguiente = claves.length;
  for (const inj of diseno.injertos) for (let k = 0; k < inj.nodos; k++) claves.push(`vm${++siguiente}`);
  return claves;
}

function poblarSelectorSobre() {
  const sel = document.getElementById("inj-sobre");
  const actual = sel.value; sel.innerHTML = "";
  for (const c of nodosBaseActuales()) sel.appendChild(new Option(c, c));
  if ([...sel.options].some(o => o.value === actual)) sel.value = actual;
  poblarSelectoresDeNodo();
}
function poblarSelectoresDeNodo() {
  for (const id of ["cap-nodo", "net-nodo"]) {
    const sel = document.getElementById(id);
    const actual = sel.value; sel.innerHTML = "";
    for (const c of todasLasClaves()) sel.appendChild(new Option(c, c));
    if ([...sel.options].some(o => o.value === actual)) sel.value = actual;
  }
}

function agregarInjerto() {
  const sobre = document.getElementById("inj-sobre").value;
  const tipo = document.getElementById("inj-tipo").value;
  const nodos = parseInt(document.getElementById("inj-nodos").value, 10) || 1;
  const ramificacion = parseInt(document.getElementById("inj-ramificacion").value, 10) || 2;
  if (!sobre || !tipo) return;
  diseno.injertos.push({ sobre, tipo, nodos, ramificacion });
  renderInjertos(); poblarSelectoresDeNodo(); redibujarLienzo();
}
function renderInjertos() {
  const cont = document.getElementById("d-lista-injertos");
  cont.innerHTML = "";
  diseno.injertos.forEach((inj, i) => {
    const fila = document.createElement("div");
    fila.className = "fila-chip";
    fila.innerHTML = `<span>sobre <b>${inj.sobre}</b>: ${inj.tipo}, ${inj.nodos} nodo(s)${inj.tipo === "arbol" ? `, ramif. ${inj.ramificacion}` : ""}</span>`;
    const btn = document.createElement("button");
    btn.className = "secundario chico"; btn.textContent = "Quitar";
    btn.addEventListener("click", () => { diseno.injertos.splice(i, 1); renderInjertos(); poblarSelectoresDeNodo(); redibujarLienzo(); });
    fila.appendChild(btn); cont.appendChild(fila);
  });
  document.getElementById("contador-injertos").textContent = diseno.injertos.length ? `(${diseno.injertos.length})` : "";
}

function poblarSelectoresDeNodoYContador() { poblarSelectoresDeNodo(); }

function agregarCapacidad() {
  const nodo = document.getElementById("cap-nodo").value;
  if (!nodo) return;
  const vcpu = document.getElementById("cap-vcpu").value, ram_mb = document.getElementById("cap-ram").value, disco_gb = document.getElementById("cap-disco").value;
  if (!vcpu && !ram_mb && !disco_gb) return;
  diseno.capacidad = diseno.capacidad.filter(c => c.nodo !== nodo);
  diseno.capacidad.push({ nodo, vcpu, ram_mb, disco_gb });
  document.getElementById("cap-vcpu").value = ""; document.getElementById("cap-ram").value = ""; document.getElementById("cap-disco").value = "";
  renderCapacidad();
}
function renderCapacidad() {
  const cont = document.getElementById("d-lista-capacidad");
  cont.innerHTML = "";
  diseno.capacidad.forEach((c, i) => {
    const partes = [];
    if (c.vcpu) partes.push(`${c.vcpu} vCPU`);
    if (c.ram_mb) partes.push(`${c.ram_mb} MB RAM`);
    if (c.disco_gb) partes.push(`${c.disco_gb} GB disco`);
    const fila = document.createElement("div");
    fila.className = "fila-chip";
    fila.innerHTML = `<span><b>${c.nodo}</b>: ${partes.join(", ")}</span>`;
    const btn = document.createElement("button");
    btn.className = "secundario chico"; btn.textContent = "Quitar";
    btn.addEventListener("click", () => { diseno.capacidad.splice(i, 1); renderCapacidad(); });
    fila.appendChild(btn); cont.appendChild(fila);
  });
  actualizarContadorOpciones();
}
function agregarInternet() {
  const nodo = document.getElementById("net-nodo").value;
  if (!nodo) return;
  diseno.internet.add(nodo); renderInternet();
}
function renderInternet() {
  const cont = document.getElementById("d-lista-internet");
  cont.innerHTML = "";
  [...diseno.internet].forEach(nodo => {
    const fila = document.createElement("div");
    fila.className = "fila-chip";
    fila.innerHTML = `<span><b>${nodo}</b>: acceso a internet</span>`;
    const btn = document.createElement("button");
    btn.className = "secundario chico"; btn.textContent = "Quitar";
    btn.addEventListener("click", () => { diseno.internet.delete(nodo); renderInternet(); });
    fila.appendChild(btn); cont.appendChild(fila);
  });
  actualizarContadorOpciones();
}
function actualizarContadorOpciones() {
  const n = diseno.capacidad.length + diseno.internet.size;
  document.getElementById("contador-opciones").textContent = n ? `(${n})` : "";
}

// ---- lienzo interactivo ----
function posicionesCirculo(claves, cx, cy, radio) {
  const pos = {};
  claves.forEach((c, i) => {
    const ang = (2 * Math.PI * i) / claves.length - Math.PI / 2;
    pos[c] = [cx + radio * Math.cos(ang), cy + radio * Math.sin(ang)];
  });
  return pos;
}

let nodoSeleccionado = null;

const NS_SVG = "http://www.w3.org/2000/svg";
function _el(tag, attrs) {
  const e = document.createElementNS(NS_SVG, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}

// Ícono genérico de nodo (rack de servidor): cuerpo con dos franjas y
// un indicador de estado. No representa ningún host real, solo distingue
// visualmente "esto es una VM" dentro del lienzo.
function _iconoServidor(cx, cyTop, clase) {
  const w = 26, h = 22;
  const x = cx - w / 2, y = cyTop;
  const g = _el("g", { class: "nodo-icono " + clase });
  g.appendChild(_el("rect", { x, y, width: w, height: h, rx: 2.5, class: "nodo-icono-cuerpo" }));
  g.appendChild(_el("line", { x1: x + 3, y1: y + h / 2, x2: x + w - 3, y2: y + h / 2, class: "nodo-icono-franja" }));
  g.appendChild(_el("circle", { cx: x + w - 6, cy: y + 5.5, r: 1.6, class: "nodo-icono-led" }));
  g.appendChild(_el("circle", { cx: x + w - 6, cy: y + h - 5.5, r: 1.6, class: "nodo-icono-led" }));
  return g;
}

function dibujarChip(x, y, texto, seleccionado, esInjerto) {
  const ancho = Math.max(56, texto.length * 8 + 20), alto = 56;
  const claseEstado = (seleccionado ? " seleccionado" : "") + (esInjerto ? " injerto" : "");
  const g = _el("g", {});
  const rect = _el("rect", {
    x: x - ancho / 2, y: y - alto / 2, width: ancho, height: alto, rx: 8,
    class: "nodo-chip" + claseEstado
  });
  g.appendChild(rect);
  g.appendChild(_iconoServidor(x, y - alto / 2 + 7, claseEstado.trim()));
  const txt = _el("text", { x, y: y + alto / 2 - 9, class: "nodo-texto" });
  txt.textContent = texto;
  g.appendChild(txt);
  return g;
}

function redibujarLienzo() {
  const tipo = document.getElementById("d-tipo").value;
  const n = nodosBaseActuales().length;
  const ramificacion = parseInt(document.getElementById("d-ramificacion").value, 10) || 2;
  const claves = nodosBaseActuales();
  const base = construirAristasBase(tipo, n, ramificacion).map(([a, b]) => claveArista(a, b));
  const baseSet = new Set(base);

  const g = document.querySelector("#d-lienzo .capa-lienzo");
  g.innerHTML = "";
  const pos = posicionesCirculo(claves, 500, 300, Math.min(240, 90 + n * 9));

  const activas = new Set(base.filter(k => !diseno.enlacesQuitados.has(k)));
  diseno.enlacesExtra.forEach(k => activas.add(k));

  for (const k of activas) {
    const [a, b] = k.split("|");
    if (!pos[a] || !pos[b]) continue;
    const linea = document.createElementNS("http://www.w3.org/2000/svg", "line");
    linea.setAttribute("x1", pos[a][0]); linea.setAttribute("y1", pos[a][1]);
    linea.setAttribute("x2", pos[b][0]); linea.setAttribute("y2", pos[b][1]);
    linea.setAttribute("class", "enlace-linea" + (baseSet.has(k) ? "" : " extra"));
    g.appendChild(linea);
  }

  claves.forEach(c => {
    const [x, y] = pos[c];
    const nodo = dibujarChip(x, y, c, c === nodoSeleccionado, false);
    nodo.classList.add("nodo-grupo");
    nodo.setAttribute("tabindex", "0");
    nodo.setAttribute("role", "button");
    nodo.setAttribute("aria-label", `Nodo ${c}${c === nodoSeleccionado ? ", seleccionado" : ""}`);
    nodo.style.cursor = "pointer";
    nodo.addEventListener("click", () => onClicNodoLienzo(c));
    nodo.addEventListener("keydown", ev => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); onClicNodoLienzo(c); }
    });
    g.appendChild(nodo);
  });

  aplicarTransform("d-lienzo");
}

function onClicNodoLienzo(clave) {
  if (nodoSeleccionado === null) nodoSeleccionado = clave;
  else if (nodoSeleccionado === clave) nodoSeleccionado = null;
  else {
    const k = claveArista(nodoSeleccionado, clave);
    const tipo = document.getElementById("d-tipo").value;
    const n = nodosBaseActuales().length;
    const ramificacion = parseInt(document.getElementById("d-ramificacion").value, 10) || 2;
    const baseSet = new Set(construirAristasBase(tipo, n, ramificacion).map(([a, b]) => claveArista(a, b)));
    if (baseSet.has(k)) {
      if (diseno.enlacesQuitados.has(k)) diseno.enlacesQuitados.delete(k); else diseno.enlacesQuitados.add(k);
    } else {
      if (diseno.enlacesExtra.has(k)) diseno.enlacesExtra.delete(k); else diseno.enlacesExtra.add(k);
    }
    nodoSeleccionado = null;
  }
  redibujarLienzo();
}

function dibujarPreview() {
  const tipo = document.getElementById("d-tipo").value;
  const n = nodosBaseActuales().length;
  const ramificacion = parseInt(document.getElementById("d-ramificacion").value, 10) || 2;
  const baseClaves = nodosBaseActuales();
  const baseAristas = construirAristasBase(tipo, n, ramificacion).map(([a, b]) => claveArista(a, b));
  const aristas = new Set(baseAristas.filter(k => !diseno.enlacesQuitados.has(k)));
  diseno.enlacesExtra.forEach(k => aristas.add(k));

  const todasClaves = [...baseClaves];
  let siguiente = baseClaves.length;
  for (const inj of diseno.injertos) {
    const ids = [inj.sobre];
    for (let k = 0; k < inj.nodos; k++) ids.push(`vm${++siguiente}`);
    for (const [a, b] of construirAristasSobreIds(inj.tipo, ids, inj.ramificacion)) aristas.add(claveArista(a, b));
    todasClaves.push(...ids.slice(1));
  }

  const g = document.querySelector("#d-preview .capa-lienzo");
  g.innerHTML = "";
  const pos = posicionesCirculo(todasClaves, 500, 320, Math.min(260, 100 + todasClaves.length * 7));
  for (const k of aristas) {
    const [a, b] = k.split("|");
    if (!pos[a] || !pos[b]) continue;
    const linea = document.createElementNS("http://www.w3.org/2000/svg", "line");
    linea.setAttribute("x1", pos[a][0]); linea.setAttribute("y1", pos[a][1]);
    linea.setAttribute("x2", pos[b][0]); linea.setAttribute("y2", pos[b][1]);
    linea.setAttribute("class", "enlace-linea");
    g.appendChild(linea);
  }
  todasClaves.forEach(c => g.appendChild(dibujarChip(pos[c][0], pos[c][1], c, false, !baseClaves.includes(c))));
  aplicarTransform("d-preview");
}

// ------------------------------------------------------------------
// Crear slice
// ------------------------------------------------------------------
async function onCrearSlice() {
  if (enviandoSlice) return;
  const msg = document.getElementById("d-mensaje");
  msg.className = "mensaje oculto";
  if (!validarFormulario()) return;

  const boton = document.getElementById("d-crear");
  enviandoSlice = true; boton.disabled = true; const textoOriginal = boton.textContent; boton.textContent = "Creando…";
  try {
    const nombre = document.getElementById("d-nombre").value.trim();
    const tipo = document.getElementById("d-tipo").value;
    const nodos = parseInt(document.getElementById("d-nodos").value, 10);
    const ramificacion = parseInt(document.getElementById("d-ramificacion").value, 10) || 2;
    const zona = document.getElementById("d-zona").value || null;
    const desplegar = document.getElementById("d-desplegar").checked;

    const enlaces_extra = [...diseno.enlacesExtra].map(k => k.split("|"));
    const enlaces_quitados = [...diseno.enlacesQuitados].map(k => k.split("|"));
    const capacidad = {};
    for (const c of diseno.capacidad) {
      const v = {};
      if (c.vcpu) v.vcpu = parseInt(c.vcpu, 10);
      if (c.ram_mb) v.ram_mb = parseInt(c.ram_mb, 10);
      if (c.disco_gb) v.disco_gb = parseFloat(c.disco_gb);
      if (Object.keys(v).length) capacidad[c.nodo] = v;
    }

    const spec = {
      nombre,
      topologia: {
        base: { tipo, nodos, ramificacion },
        injertos: diseno.injertos.map(i => ({ sobre: i.sobre, tipo: i.tipo, nodos: i.nodos, ramificacion: i.ramificacion })),
        enlaces_extra, enlaces_quitados,
      },
      flavores: {}, acceso_internet: [...diseno.internet], capacidad,
      zona_disponibilidad: zona, desplegar, estrategia: "objetivo",
      asincrono: desplegar,        // el despliegue se encola (R1) y la API responde al instante
    };

    const resultado = await api("POST", "/slices", spec);
    if (resultado.trabajo) {
      const t = resultado.trabajo;
      msg.textContent = `Slice "${resultado.slice.nombre}" creado; despliegue en cola ` +
        `(${t.id}, ${t.posicion ? "posición " + t.posicion : "en curso"}). Sigue el avance en la pestaña Trabajos.`;
    } else {
      msg.textContent = `Slice "${resultado.nombre}" creado (${resultado.estado}).`;
    }
    msg.className = "mensaje ok";
    cargarSlices(); cargarRecursos(); cargarTrabajos();
  } catch (e) {
    msg.textContent = e.message;
    msg.className = "mensaje error";
  } finally {
    enviandoSlice = false; boton.disabled = false; boton.textContent = textoOriginal;
  }
}

// ------------------------------------------------------------------
// Plantillas
// ------------------------------------------------------------------
async function exportarPlantilla(id, nombre) {
  try {
    const plantilla = await api("GET", `/slices/${id}/plantilla`);
    const blob = new Blob([JSON.stringify(plantilla, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `${nombre}-plantilla.json`;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert("No se pudo exportar: " + e.message);
  }
}

async function onCrearDesdePlantilla() {
  const msg = document.getElementById("d-mensaje");
  msg.className = "mensaje oculto";
  const archivo = document.getElementById("plant-archivo").files[0];
  if (!archivo) { alert("Elige primero un archivo .json de plantilla."); return; }
  try {
    const plantilla = JSON.parse(await archivo.text());
    const nombre = document.getElementById("plant-nombre").value.trim() || undefined;
    const resultado = await api("POST", "/plantillas", { plantilla, nombre });
    msg.textContent = `Slice "${resultado.nombre}" creado desde la plantilla (${resultado.estado}).`;
    msg.className = "mensaje ok";
    document.getElementById("plant-archivo").value = ""; document.getElementById("plant-nombre").value = "";
    cargarSlices(); cargarRecursos();
  } catch (e) {
    msg.textContent = "Error al importar la plantilla: " + e.message;
    msg.className = "mensaje error";
  }
}

// ------------------------------------------------------------------
// Barra superior: menú lateral, menú de cuenta y cambio de contraseña
// ------------------------------------------------------------------
function configurarBarraYMenu() {
  document.getElementById("btn-menu").addEventListener("click", () => {
    if (window.matchMedia("(max-width: 900px)").matches) document.body.classList.toggle("menu-abierto");
    else document.body.classList.toggle("menu-cerrado");
  });
  const btn = document.getElementById("btn-usuario"), menu = document.getElementById("menu-usuario");
  btn.addEventListener("click", ev => {
    ev.stopPropagation();
    const abrir = menu.classList.contains("oculto");
    menu.classList.toggle("oculto", !abrir);
    btn.setAttribute("aria-expanded", String(abrir));
  });
  document.addEventListener("click", ev => { if (!ev.target.closest(".usuario-menu")) cerrarMenuUsuario(); });
  document.addEventListener("keydown", ev => { if (ev.key === "Escape") cerrarMenuUsuario(); });

  const dlg = document.getElementById("dlg-clave");
  document.getElementById("btn-cambiar-clave").addEventListener("click", () => {
    cerrarMenuUsuario();
    for (const id of ["clave-actual", "clave-nueva", "clave-repetir"]) document.getElementById(id).value = "";
    document.getElementById("clave-mensaje").className = "mensaje oculto";
    dlg.showModal();
    document.getElementById("clave-actual").focus();
  });
  for (const id of ["clave-cerrar", "clave-cancelar"]) document.getElementById(id).addEventListener("click", () => dlg.close());
  document.getElementById("form-clave").addEventListener("submit", onCambiarClave);
}

function cerrarMenuUsuario() {
  document.getElementById("menu-usuario").classList.add("oculto");
  document.getElementById("btn-usuario").setAttribute("aria-expanded", "false");
}

async function onCambiarClave(ev) {
  ev.preventDefault();
  const msg = document.getElementById("clave-mensaje");
  const actual = document.getElementById("clave-actual").value;
  const nueva = document.getElementById("clave-nueva").value;
  const repetir = document.getElementById("clave-repetir").value;
  const fallar = texto => { msg.textContent = texto; msg.className = "mensaje error"; };
  if (!actual) return fallar("Escribe tu contraseña actual.");
  if (nueva.length < 8) return fallar("La contraseña nueva debe tener al menos 8 caracteres.");
  if (nueva !== repetir) return fallar("Las dos contraseñas nuevas no coinciden.");
  const boton = document.getElementById("clave-guardar");
  boton.disabled = true;
  try {
    await api("POST", "/cuenta/contrasena", { actual, nueva });
    msg.textContent = "Contraseña actualizada.";
    msg.className = "mensaje ok";
    setTimeout(() => document.getElementById("dlg-clave").close(), 1200);
  } catch (e) {
    fallar(e.message);
  } finally {
    boton.disabled = false;
  }
}

// ------------------------------------------------------------------
// Slices: lista con búsqueda
// ------------------------------------------------------------------
const ICONO = nombre => `<svg class="ico"><use href="#i-${nombre}"/></svg>`;

async function cargarSlices() {
  try {
    const ruta = estado.rol === "admin" && estado.verTodos ? "/slices?todos=1" : "/slices";
    estado.slices = await api("GET", ruta);
    renderTablaSlices();
  } catch (e) {
    if (/401|token/i.test(e.message)) onSalir();
  }
}

function renderTablaSlices() {
  const tabla = document.getElementById("tabla-slices");
  const conDueno = estado.slices.some(s => s.owner);
  tabla.classList.toggle("sin-owner", !conDueno);
  const f = estado.filtroSlices;
  const visibles = estado.slices.filter(s => !f ||
    [s.nombre, s.id, s.tipo, s.estado, s.owner, s.zona].some(v => String(v ?? "").toLowerCase().includes(f)));
  const tbody = tabla.querySelector("tbody");
  tbody.innerHTML = "";
  for (const s of visibles) {
    const tr = document.createElement("tr");
    const estadoSl = String(s.estado).toLowerCase();
    tr.innerHTML = `<td><button type="button" class="enlace-slice">${esc(s.nombre)}</button></td>
      <td class="col-owner"><span class="dueno">${esc(s.owner || "-")}</span></td>
      <td>${esc(s.tipo)}</td><td>${esc(s.nodos)}</td><td>${esc(s.enlaces ?? "-")}</td>
      <td>${esc(s.zona || "-")}</td>
      <td><span class="estado-pill estado-${esc(estadoSl)}">${esc(s.estado)}</span></td>
      <td class="acciones"></td>`;
    tr.querySelector(".enlace-slice").addEventListener("click", () => verSlice(s.id));
    const acc = tr.querySelector(".acciones");
    const boton = (icono, titulo, fn, clase = "") => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "btn-accion " + clase; b.title = titulo; b.setAttribute("aria-label", `${titulo} ${s.nombre}`);
      b.innerHTML = ICONO(icono); b.addEventListener("click", fn); acc.appendChild(b);
    };
    boton("ojo", "Ver topología", () => verSlice(s.id));
    if (["borrador", "error"].includes(estadoSl)) boton("play", "Desplegar", () => desplegarSlice(s.id, s.nombre), "principal");
    boton("basura", "Eliminar", () => eliminarSlice(s.id, s.nombre), "peligro");
    tbody.appendChild(tr);
  }
  document.getElementById("slices-vacio").classList.toggle("oculto", estado.slices.length > 0);
  document.getElementById("slices-conteo").textContent = estado.slices.length
    ? `Mostrando ${visibles.length} de ${estado.slices.length} slice${estado.slices.length === 1 ? "" : "s"}` : "";
}

// ------------------------------------------------------------------
// Detalle: grafo del slice con la VM en su servidor y la VLAN de cada enlace
// ------------------------------------------------------------------
const COLORES_HOST = ["#1f6fd6", "#c2410c", "#7c3aed", "#0f766e", "#be185d", "#4d7c0f", "#475569"];

function hostsDe(s) { return [...new Set(s.nodos.map(n => n.host).filter(Boolean))].sort(); }
function colorHost(hosts, h) { const i = hosts.indexOf(h); return i < 0 ? "#64748b" : COLORES_HOST[i % COLORES_HOST.length]; }

async function verSlice(id) {
  activarTab("detalle");
  const info = document.getElementById("det-info");
  try {
    const s = await api("GET", "/slices/" + id);
    estado.detalle = s;
    if (!s.nodos.some(n => n.clave === estado.nodoDetalle)) estado.nodoDetalle = null;
    renderDetalle(s);
  } catch (e) {
    estado.detalle = null;
    document.getElementById("det-nombre").textContent = "";
    document.querySelector("#det-lienzo .capa-lienzo").innerHTML = "";
    info.innerHTML = `<p class="error">${esc(e.message)}</p>`;
  }
}

function renderDetalle(s) {
  document.getElementById("det-nombre").textContent = s.nombre;
  const hosts = hostsDe(s);
  const pills = [`<span class="estado-pill estado-${esc(s.estado)}">${esc(s.estado)}</span>`,
    `<span class="chip">${esc(s.tipo)}</span>`,
    `<span class="chip">${s.nodos.length} VMs · ${s.enlaces.length} enlaces</span>`];
  if (s.zona) pills.push(`<span class="chip">zona: ${esc(s.zona)}</span>`);
  if (s.vlan_servicio != null) pills.push(`<span class="chip" title="Etiqueta externa QinQ (802.1ad) del slice">S-VLAN ${esc(s.vlan_servicio)}</span>`);
  if (estado.rol === "admin" && s.owner) pills.push(`<span class="chip">propietario: ${esc(s.owner)}</span>`);
  document.getElementById("det-pills").innerHTML = pills.join("");
  document.getElementById("det-desplegar").classList.toggle("oculto", !["borrador", "error"].includes(String(s.estado).toLowerCase()));

  const leyenda = document.getElementById("det-leyenda");
  leyenda.innerHTML = hosts.length ? hosts.map(h =>
      `<div class="fila"><span class="marca-host" style="background:${colorHost(hosts, h)}"></span>${esc(h)}</div>`).join("") +
    `<div class="fila"><span class="linea cruza"></span>enlace entre servidores</div>
     <div class="fila"><span class="linea"></span>enlace dentro del servidor</div>` : "";

  dibujarSlice(s, hosts);
  renderInfoNodo(s);
  renderTablasDetalle(s);
  requestAnimationFrame(() => ajustarVista("det-lienzo"));
}

function posicionesPorHost(s, hosts) {
  const pos = {}, cajas = [];
  const celdaW = 150, celdaH = 130, titulo = 40, margen = 24, sep = 110;
  const grupos = hosts.map(h => [h, s.nodos.filter(n => n.host === h)]);
  const sinHost = s.nodos.filter(n => !n.host);
  if (sinHost.length) grupos.push([null, sinHost]);
  // Tamaño de cada caja según cuántas VMs tiene; las cajas se reparten en un
  // círculo para que un enlace entre dos servidores no pase sobre un tercero.
  const medidas = grupos.map(([, vms]) => {
    const cols = Math.max(1, Math.ceil(Math.sqrt(vms.length)));
    const filas = Math.ceil(vms.length / cols);
    return { cols, w: cols * celdaW + margen * 2, alto: filas * celdaH + titulo + margen };
  });
  const k = grupos.length;
  const mayor = Math.max(...medidas.map(m => Math.hypot(m.w, m.alto)));
  const radio = k === 1 ? 0 : k === 2 ? (mayor + sep) / 2 : (mayor + sep) / (2 * Math.sin(Math.PI / k));
  grupos.forEach(([h, vms], i) => {
    const ang = k === 2 ? Math.PI * i : (2 * Math.PI * i) / k - Math.PI / 2;
    const { cols, w, alto } = medidas[i];
    const x0 = radio * Math.cos(ang) - w / 2, y0 = radio * Math.sin(ang) - alto / 2;
    cajas.push({ host: h, x: x0, y: y0, w, h: alto });
    vms.forEach((n, j) => {
      pos[n.clave] = [x0 + margen + celdaW * (j % cols) + celdaW / 2,
                      y0 + titulo + celdaH * Math.floor(j / cols) + celdaH / 2];
    });
  });
  return { pos, cajas };
}

function dibujarSlice(s, hosts) {
  const g = document.querySelector("#det-lienzo .capa-lienzo");
  g.innerHTML = "";
  const claves = s.nodos.map(n => n.clave);
  const hostDe = Object.fromEntries(s.nodos.map(n => [n.clave, n.host]));
  let pos, cajas = [];
  if (hosts.length) ({ pos, cajas } = posicionesPorHost(s, hosts));
  else pos = posicionesCirculo(claves, 500, 310, Math.min(260, 110 + claves.length * 12));

  for (const c of cajas) {
    const caja = _el("g", { style: `color:${c.host ? colorHost(hosts, c.host) : "#64748b"}` });
    caja.appendChild(_el("rect", { x: c.x, y: c.y, width: c.w, height: c.h, rx: 14, class: "host-caja" }));
    const ico = _el("g", { class: "host-icono", transform: `translate(${c.x + 14},${c.y + 10})` });
    ico.appendChild(_el("rect", { x: 0, y: 0, width: 16, height: 20, rx: 2 }));
    for (const y of [6, 11, 16]) ico.appendChild(_el("line", { x1: 3, y1: y, x2: 13, y2: y }));
    caja.appendChild(ico);
    const t = _el("text", { x: c.x + 38, y: c.y + 25, class: "host-titulo" });
    t.textContent = c.host || "sin servidor asignado";
    caja.appendChild(t);
    g.appendChild(caja);
  }

  const etiquetas = [];
  for (const e of s.enlaces) {
    const a = pos[e.a], b = pos[e.b];
    if (!a || !b) continue;
    const cruza = hosts.length > 0 && hostDe[e.a] !== hostDe[e.b];
    g.appendChild(_el("line", { x1: a[0], y1: a[1], x2: b[0], y2: b[1], class: "enlace-det" + (cruza ? " cruza" : "") }));
    if (e.vlan != null) etiquetas.push([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, `VLAN ${e.vlan}`]);
  }
  for (const [x, y, texto] of etiquetas) {
    const et = _el("g", { class: "etiqueta-vlan" });
    const ancho = texto.length * 6.6 + 10;
    et.appendChild(_el("rect", { x: x - ancho / 2, y: y - 9, width: ancho, height: 18, rx: 9 }));
    const t = _el("text", { x, y }); t.textContent = texto; et.appendChild(t);
    g.appendChild(et);
  }

  for (const n of s.nodos) {
    const [x, y] = pos[n.clave];
    const color = n.host ? colorHost(hosts, n.host) : "#64748b";
    const ip = (n.interfaces && n.interfaces[0] && n.interfaces[0].ip) || "";
    const vm = _el("g", {
      class: "vm-grupo" + (n.clave === estado.nodoDetalle ? " sel" : "") +
             (String(n.estado).toLowerCase() === "error" ? " vm-estado-error" : ""),
      transform: `translate(${x},${y})`, style: `color:${color}`, tabindex: "0", role: "button",
      "aria-label": `VM ${n.clave}${n.host ? " en " + n.host : ""}`,
    });
    vm.appendChild(_el("rect", { x: -46, y: -38, width: 92, height: 78, rx: 12, class: "vm-fondo" }));
    vm.appendChild(_el("rect", { x: -18, y: -29, width: 36, height: 24, rx: 2.5, class: "vm-pantalla" }));
    vm.appendChild(_el("rect", { x: -15, y: -26, width: 30, height: 18, rx: 1.5, class: "vm-brillo" }));
    vm.appendChild(_el("rect", { x: -4, y: -5, width: 8, height: 5, class: "vm-base" }));
    vm.appendChild(_el("rect", { x: -11, y: 0, width: 22, height: 3, rx: 1.5, class: "vm-base" }));
    const t = _el("text", { x: 0, y: 18, class: "vm-texto" }); t.textContent = n.clave; vm.appendChild(t);
    const sub = _el("text", { x: 0, y: 31, class: "vm-sub" });
    sub.textContent = ip ? ip.split("/")[0] : (n.flavor || `${n.vcpu} vCPU`);
    vm.appendChild(sub);
    if (n.acceso_internet) {
      const net = _el("g", { class: "vm-internet", transform: "translate(38,-30)" });
      net.appendChild(_el("circle", { cx: 0, cy: 0, r: 9 }));
      net.appendChild(_el("path", { d: "M-5 0h10M0-5c2 1.8 2 8.2 0 10M0-5c-2 1.8-2 8.2 0 10" }));
      const tt = _el("title", {}); tt.textContent = "Con salida a Internet"; net.appendChild(tt);
      vm.appendChild(net);
    }
    const elegir = () => { estado.nodoDetalle = n.clave; dibujarSlice(s, hosts); renderInfoNodo(s); };
    vm.addEventListener("click", elegir);
    vm.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); elegir(); } });
    g.appendChild(vm);
  }
  aplicarTransform("det-lienzo");
}

function renderInfoNodo(s) {
  const cont = document.getElementById("det-info");
  const n = s.nodos.find(x => x.clave === estado.nodoDetalle);
  if (!n) {
    cont.innerHTML = `<p class="vacio">Selecciona una VM en el dibujo para ver su información.</p>`;
    return;
  }
  const hosts = hostsDe(s);
  const vecinos = s.enlaces.filter(e => e.a === n.clave || e.b === n.clave)
    .map(e => `<li>${esc(e.a === n.clave ? e.b : e.a)} — VLAN ${esc(e.vlan ?? "-")}</li>`).join("");
  const itfs = (n.interfaces || []).map(i => `<li>${esc(i.ip)} (VLAN ${esc(i.vlan)})</li>`).join("");
  const activo = String(s.estado).toLowerCase() === "activo";
  cont.innerHTML = `
    <h3>${esc(n.clave)}</h3>
    <p class="sub">${esc(n.nombre || "")}</p>
    <dl>
      <dt>Estado</dt><dd><span class="estado-pill estado-${esc(String(n.estado || s.estado).toLowerCase())}">${esc(n.estado || s.estado)}</span></dd>
      <dt>Servidor</dt><dd>${n.host ? `<span class="marca-host" style="background:${colorHost(hosts, n.host)}"></span>${esc(n.host)}` : "-"}</dd>
      <dt>Flavor</dt><dd>${esc(n.flavor || "-")}</dd>
      <dt>vCPU</dt><dd>${esc(n.vcpu)}</dd>
      <dt>RAM</dt><dd>${esc(n.ram_mb)} MB</dd>
      <dt>Disco</dt><dd>${esc(n.disco_gb)} GB</dd>
      <dt>Imagen</dt><dd>${esc(n.imagen || "-")}</dd>
      <dt>Internet</dt><dd>${n.acceso_internet ? "sí" : "no"}</dd>
      ${n.ip_acceso ? `<dt>IP acceso</dt><dd>${esc(n.ip_acceso)}</dd>` : ""}
      ${n.puerto_ssh ? `<dt>SSH</dt><dd>puerto ${esc(n.puerto_ssh)}</dd>` : ""}
    </dl>
    ${itfs ? `<h4>Interfaces</h4><ul>${itfs}</ul>` : ""}
    ${vecinos ? `<h4>Conectada con</h4><ul>${vecinos}</ul>` : ""}
    <button type="button" class="primario ancho" id="btn-consola" ${activo ? "" : "disabled"}
      title="${activo ? "Emitir un token de un solo uso para la consola" : "Solo para slices activos"}">${ICONO("consola")} Abrir consola</button>
    <div id="caja-consola"></div>`;
  document.getElementById("btn-consola").addEventListener("click", () => pedirConsola(s.id, n.clave));
}

async function pedirConsola(sliceId, clave) {
  const caja = document.getElementById("caja-consola");
  caja.innerHTML = "";
  try {
    const r = await api("POST", `/slices/${sliceId}/consola`, { nodo: clave });
    const url = r.url_consola_real
      ? `<a href="${esc(r.url_consola_real)}" target="_blank" rel="noopener">Abrir la consola noVNC</a><br>` : "";
    caja.innerHTML = `<div class="caja-consola">${url}servidor: ${esc(r.host)} · puerto VNC ${esc(r.puerto)}<br>
      token (un solo uso, vence en ${esc(r.expira_en)} s):<br>${esc(r.token)}</div>`;
  } catch (e) {
    caja.innerHTML = `<p class="mensaje error">${esc(e.message)}</p>`;
  }
}

function renderTablasDetalle(s) {
  const filasNodos = s.nodos.map(n => `<tr><td>${esc(n.clave)}</td><td>${esc(n.estado || "-")}</td>
    <td>${esc(n.flavor || "-")}</td><td>${esc(n.vcpu)}</td><td>${esc(n.ram_mb)}</td><td>${esc(n.disco_gb)}</td>
    <td>${esc(n.host || "-")}</td><td>${esc((n.interfaces || []).map(i => i.ip).join(", ") || "-")}</td></tr>`).join("");
  const filasEnlaces = s.enlaces.map(e => `<tr><td>${esc(e.a)} — ${esc(e.b)}</td><td>${esc(e.vlan ?? "-")}</td>
    <td>${esc(e.red || "-")}</td></tr>`).join("");
  document.getElementById("det-tablas").innerHTML = `
    <h4>Nodos</h4><div class="tabla-scroll"><table class="tabla"><thead><tr><th>Nodo</th><th>Estado</th><th>Flavor</th><th>vCPU</th>
      <th>RAM MB</th><th>Disco GB</th><th>Servidor</th><th>IPs</th></tr></thead><tbody>${filasNodos}</tbody></table></div>
    <h4>Enlaces</h4><div class="tabla-scroll"><table class="tabla"><thead><tr><th>Entre</th><th>VLAN</th><th>Red</th></tr></thead>
      <tbody>${filasEnlaces}</tbody></table></div>`;
}

async function eliminarSlice(id, nombre) {
  if (!confirm(`¿Eliminar el slice "${nombre}"? Esta acción no se puede deshacer.`)) return;
  try {
    await api("DELETE", "/slices/" + id + "?asincrono=1");   // se encola como trabajo (R1)
    await cargarTrabajos();
    activarTab("trabajos");
  } catch (e) {
    alert(e.message);
  }
}

// ------------------------------------------------------------------
// Recursos: tarjetas de resumen y barras por servidor
// ------------------------------------------------------------------
function medidor(rotulo, usado, total, unidad) {
  const pct = total ? Math.min(100, Math.round((usado / total) * 100)) : 0;
  const clase = pct >= 90 ? " lleno" : pct >= 70 ? " alto" : "";
  return `<div class="medidor"><div class="rotulo"><span>${esc(rotulo)}</span><span>${esc(usado)} / ${esc(total)} ${unidad} (${pct}%)</span></div>
    <div class="pista"><div class="relleno${clase}" style="width:${pct}%"></div></div></div>`;
}

async function cargarRecursos() {
  try {
    const r = await api("GET", "/recursos");
    const suma = (lista, campo) => lista.reduce((acc, x) => acc + (Number(x[campo]) || 0), 0);
    const tarjetas = [
      ["slices", r.mis_slices.length, "slices activos tuyos"],
      ["disenar", suma(r.mis_slices, "nodos"), "VMs desplegadas tuyas"],
      ["recursos", `${suma(r.hosts, "vcpu_usados")} / ${suma(r.hosts, "vcpu_total")}`, "vCPU en uso en el clúster"],
      ["red", r.hosts.length, "servidores disponibles"],
    ];
    document.getElementById("resumen-recursos").innerHTML = tarjetas.map(([ico, valor, etiqueta]) =>
      `<div class="resumen"><div class="icono-res">${ICONO(ico)}</div><div><div class="valor">${esc(valor)}</div>
       <div class="etiqueta">${esc(etiqueta)}</div></div></div>`).join("");
    document.getElementById("barras-hosts").innerHTML = r.hosts.length ? r.hosts.map(h => `
      <div class="barra-host">
        <div class="nombre">${esc(h.host)}<small>${esc(h.zona || "")} · disco usado ${esc(h.disco_usado_gb)} GB</small></div>
        ${medidor("vCPU", h.vcpu_usados, h.vcpu_total, "")}
        ${medidor("RAM", h.ram_usada_mb, h.ram_total_mb, "MB")}
        <div></div>
      </div>`).join("") : `<p class="vacio">No se detectaron servidores.</p>`;
    document.querySelector("#tabla-mis-slices tbody").innerHTML = r.mis_slices.map(s => `<tr><td>${esc(s.nombre)}</td><td>${esc(s.nodos)}</td>
      <td>${esc(s.vcpu)}</td><td>${esc(s.ram_mb)}</td><td>${esc(s.disco_gb)}</td></tr>`).join("");
    document.getElementById("recursos-total").textContent =
      `Total propio: ${r.total_mio.vcpu} vCPU, ${r.total_mio.ram_mb} MB de RAM, ${r.total_mio.disco_gb} GB de disco.`;
  } catch (e) {
    if (/401|token/i.test(e.message)) onSalir();
  }
}


// ------------------------------------------------------------------
// Desplegar un borrador desde la lista (encolado)
// ------------------------------------------------------------------
async function desplegarSlice(id, nombre) {
  try {
    await api("POST", `/slices/${id}/desplegar`, { estrategia: "objetivo" });
    await cargarTrabajos();
    activarTab("trabajos");
  } catch (e) {
    alert(`No se pudo encolar el despliegue de "${nombre}": ${e.message}`);
  }
}

// ------------------------------------------------------------------
// Tab: Trabajos (cola FIFO, R1)
// ------------------------------------------------------------------
let sondeoTimer = null;
let activosPrevios = new Set();
const ESTADOS_ACTIVOS = ["pendiente", "en_curso"];

function hayTrabajosActivos() { return estado.trabajos.some(t => ESTADOS_ACTIVOS.includes(t.estado)); }

function iniciarSondeo() {
  detenerSondeo();
  const ciclo = async () => {
    if (!estado.token) return;
    if (!document.hidden) await cargarTrabajos();
    sondeoTimer = setTimeout(ciclo, hayTrabajosActivos() ? 2000 : 8000);
  };
  sondeoTimer = setTimeout(ciclo, 1500);
}

function detenerSondeo() {
  clearTimeout(sondeoTimer); sondeoTimer = null;
  activosPrevios = new Set(); estado.trabajos = [];
}

async function cargarTrabajos() {
  if (!estado.token) return;
  try {
    const ruta = estado.rol === "admin" && estado.verTodos ? "/trabajos?todos=1" : "/trabajos";
    estado.trabajos = await api("GET", ruta);
  } catch (e) {
    if (/401|token/i.test(e.message)) onSalir();
    return;
  }
  const activos = new Set(estado.trabajos.filter(t => ESTADOS_ACTIVOS.includes(t.estado)).map(t => t.id));
  // Si algún trabajo acaba de terminar, el estado de los slices y de los hosts cambió.
  if ([...activosPrevios].some(id => !activos.has(id))) { cargarSlices(); cargarRecursos(); cargarBroadcast(); }
  activosPrevios = activos;
  const badge = document.getElementById("badge-trabajos");
  badge.textContent = activos.size ? String(activos.size) : "";
  renderTablaTrabajos();
}

function renderTablaTrabajos() {
  const tbody = document.querySelector("#tabla-trabajos tbody");
  document.getElementById("trabajos-vacio").classList.toggle("oculto", estado.trabajos.length > 0);
  tbody.innerHTML = estado.trabajos.map(t => {
    const sl = estado.slices.find(x => x.id === t.slice_id);
    const dur = t.iniciado && t.terminado ? `${(t.terminado - t.iniciado).toFixed(1)} s`
              : t.estado === "en_curso" ? "en curso…" : "-";
    const detalle = t.error ? `<span class="celda-error">${esc(t.error)}</span>`
                  : t.estado === "completado" ? "ok" : "";
    return `<tr><td>${esc(t.id)}</td><td>${esc(t.tipo)}</td>
      <td>${esc(sl ? sl.nombre + " (" + t.slice_id + ")" : t.slice_id)}</td><td>${esc(t.usuario)}</td>
      <td><span class="estado-pill estado-${esc(t.estado)}">${esc(t.estado)}</span></td>
      <td>${t.posicion ? "#" + esc(t.posicion) : "-"}</td><td>${esc(dur)}</td><td>${detalle}</td></tr>`;
  }).join("");
}

// ------------------------------------------------------------------
// Tab: Red / control de broadcast (R5)
// ------------------------------------------------------------------
async function cargarBroadcast() {
  if (!estado.token) return;
  try {
    const filas = await api("GET", "/red/broadcast");
    const lista = vs => (vs && vs.length ? vs.join(", ") : "-");
    document.getElementById("red-vacio").classList.toggle("oculto", filas.length > 0);
    document.querySelector("#tabla-red tbody").innerHTML = filas.map(f => `<tr>
      <td>${esc(f.host)}</td><td>${esc(lista(f.vlans))}</td><td>${esc(lista(f.vlans_uplink))}</td>
      <td>${esc(lista(f.vlans_solo_locales))}</td><td>${esc(f.puertos_con_storm_control)}</td></tr>`).join("");
  } catch (e) {
    if (/401|token/i.test(e.message)) onSalir();
  }
}

// ------------------------------------------------------------------
// Roles y permisos (todos) y administración de usuarios (solo admin)
// ------------------------------------------------------------------
async function cargarPermisos() {
  try {
    const r = await api("GET", "/permisos");
    document.getElementById("permisos-rol").textContent = r.rol;
    document.querySelectorAll("#tabla-permisos th.col-rol").forEach(th => th.classList.toggle("mio", th.dataset.rol === r.rol));
    const marca = si => si ? '<span class="si" aria-label="sí">✓</span>' : '<span class="no" aria-label="no">—</span>';
    document.querySelector("#tabla-permisos tbody").innerHTML = r.permisos.map(p => `<tr>
      <td>${esc(p.tarea)}</td>
      <td class="${r.rol === "usuario" ? "mio" : ""}">${marca(p.usuario)}</td>
      <td class="${r.rol === "admin" ? "mio" : ""}">${marca(p.admin)}</td></tr>`).join("");
  } catch (e) {
    if (/401|token/i.test(e.message)) onSalir();
  }
}

async function cargarUsuarios() {
  if (estado.rol !== "admin") return;
  try {
    const filas = await api("GET", "/usuarios");
    const tbody = document.querySelector("#tabla-usuarios tbody");
    tbody.innerHTML = "";
    for (const u of filas) {
      const tr = document.createElement("tr");
      const yo = u.usuario === estado.usuario;
      tr.innerHTML = `<td>${esc(u.usuario)}${yo ? ' <span class="dueno">(tú)</span>' : ""}</td>
        <td><span class="chip-rol">${esc(u.rol)}</span></td><td>${esc(u.slices)}</td><td>${esc(u.activos)}</td>
        <td class="acciones"></td>`;
      if (!yo) {
        const b = document.createElement("button");
        b.type = "button"; b.className = "btn-accion peligro"; b.title = "Eliminar usuario";
        b.setAttribute("aria-label", `Eliminar usuario ${u.usuario}`);
        b.innerHTML = ICONO("basura");
        b.addEventListener("click", () => eliminarUsuario(u.usuario));
        tr.querySelector(".acciones").appendChild(b);
      }
      tbody.appendChild(tr);
    }
  } catch (e) {
    if (/401|token/i.test(e.message)) onSalir();
  }
}

async function onCrearUsuario(ev) {
  ev.preventDefault();
  const msg = document.getElementById("nu-mensaje");
  const usuario = document.getElementById("nu-usuario").value.trim();
  const contrasena = document.getElementById("nu-clave").value;
  const rol = document.getElementById("nu-rol").value;
  try {
    await api("POST", "/usuarios", { usuario, contrasena, rol });
    msg.textContent = `Usuario "${usuario}" creado con rol ${rol}.`;
    msg.className = "mensaje ok";
    document.getElementById("nu-usuario").value = ""; document.getElementById("nu-clave").value = "";
    cargarUsuarios();
  } catch (e) {
    msg.textContent = e.message;
    msg.className = "mensaje error";
  }
}

async function eliminarUsuario(usuario) {
  if (!confirm(`¿Eliminar al usuario "${usuario}"? Ya no podrá iniciar sesión.`)) return;
  try {
    await api("DELETE", "/usuarios/" + encodeURIComponent(usuario));
    cargarUsuarios();
  } catch (e) {
    alert(e.message);
  }
}

async function revisarResiduos(confirmar) {
  if (confirmar && !confirm("¿Borrar los recursos huérfanos del clúster? No se puede deshacer.")) return;
  const caja = document.getElementById("residuos-resultado");
  caja.classList.remove("oculto");
  caja.textContent = confirmar ? "Limpiando…" : "Revisando…";
  try {
    const r = await api("POST", "/mantenimiento/limpiar-residuos", { confirmar });
    caja.textContent = JSON.stringify(r, null, 2);
  } catch (e) {
    caja.textContent = e.message;
  }
}
