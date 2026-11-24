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
  document.getElementById("toggle-todos").addEventListener("change", e => { estado.verTodos = e.target.checked; cargarSlices(); cargarTrabajos(); });

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
  document.getElementById("toggle-todos-wrap").classList.toggle("oculto", estado.rol !== "admin");

  try {
    const [catalogo, zonas] = await Promise.all([api("GET", "/catalogo"), api("GET", "/zonas")]);
    estado.catalogo = catalogo; estado.zonas = zonas;
  } catch (e) {
    if (/401|token|credencial/i.test(e.message)) { onSalir(); return; }
  }
  poblarCatalogoYZonas();
  activarTab("disenar");
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
function activarTab(nombre) {
  document.querySelectorAll(".tab").forEach(b => {
    const activo = b.dataset.tab === nombre;
    b.classList.toggle("activo", activo); b.setAttribute("aria-selected", String(activo));
  });
  document.querySelectorAll(".panel-tab").forEach(p => p.classList.toggle("oculto", p.id !== "tab-" + nombre));
  if (nombre === "trabajos") cargarTrabajos();
  if (nombre === "red") cargarBroadcast();
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
};

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
    if (ev.target.closest(".nodo-grupo")) return;
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
// Tab: Mis slices
// ------------------------------------------------------------------
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
  const tbody = document.querySelector("#tabla-slices tbody");
  tbody.innerHTML = "";
  for (const s of estado.slices) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(s.nombre)}${s.owner ? ` <span class="campo-ayuda">(${esc(s.owner)})</span>` : ""}</td>
      <td><span class="estado-pill estado-${esc(s.estado)}">${esc(s.estado)}</span></td>
      <td>${esc(s.tipo)}</td><td>${esc(s.nodos)}</td><td></td>`;
    const tdAcc = tr.querySelector("td:last-child");
    const btnVer = document.createElement("button");
    btnVer.className = "secundario chico"; btnVer.textContent = "Ver";
    btnVer.addEventListener("click", () => verSlice(s.id));
    const btnDel = document.createElement("button");
    btnDel.className = "secundario chico"; btnDel.textContent = "Eliminar";
    btnDel.addEventListener("click", () => eliminarSlice(s.id, s.nombre));
    tdAcc.appendChild(btnVer);
    if (["borrador", "error"].includes(String(s.estado).toLowerCase())) {
      const btnDep = document.createElement("button");
      btnDep.className = "primario chico"; btnDep.textContent = "Desplegar";
      btnDep.addEventListener("click", () => desplegarSlice(s.id, s.nombre));
      tdAcc.appendChild(btnDep);
    }
    tdAcc.appendChild(btnDel);
    tbody.appendChild(tr);
  }
}

async function verSlice(id) {
  const cont = document.getElementById("detalle-slice");
  try {
    const s = await api("GET", "/slices/" + id);
    const filasNodos = s.nodos.map(n => `<tr><td>${n.clave}</td><td>${n.estado || "-"}</td>
      <td>${n.vcpu}</td><td>${n.ram_mb}</td><td>${n.disco_gb}</td>
      <td>${n.host || "-"}</td><td>${n.ip_acceso || "-"}</td></tr>`).join("");
    const filasEnlaces = s.enlaces.map(e => `<tr><td>${e.a} / ${e.b}</td><td>${e.vlan ?? "-"}</td></tr>`).join("");
    cont.innerHTML = `
      <div class="cabecera-detalle">
        <h3>${s.nombre}</h3>
        <button type="button" class="secundario chico" id="btn-exportar-plantilla">Exportar plantilla</button>
      </div>
      <div class="pills-detalle">
        <span class="estado-pill estado-${s.estado}">${s.estado}</span>
        <span class="chip">${s.tipo}</span>
        ${s.zona ? `<span class="chip">zona: ${s.zona}</span>` : ""}
      </div>
      <h4>Nodos</h4>
      <table><thead><tr><th>Nodo</th><th>Estado</th><th>vCPU</th><th>RAM</th><th>Disco</th><th>Host</th><th>IP</th></tr></thead>
      <tbody>${filasNodos}</tbody></table>
      <h4>Enlaces</h4>
      <table><thead><tr><th>Entre</th><th>VLAN</th></tr></thead><tbody>${filasEnlaces}</tbody></table>`;
    document.getElementById("btn-exportar-plantilla").addEventListener("click", () => exportarPlantilla(id, s.nombre));
  } catch (e) {
    cont.innerHTML = `<p class="error">${e.message}</p>`;
  }
}

async function eliminarSlice(id, nombre) {
  if (!confirm(`¿Eliminar el slice "${nombre}"? Esta acción no se puede deshacer.`)) return;
  try {
    await api("DELETE", "/slices/" + id + "?asincrono=1");   // se encola como trabajo (R1)
    cargarTrabajos();
    document.getElementById("detalle-slice").innerHTML = '<p class="campo-ayuda">Eliminación en cola: sigue el avance en la pestaña Trabajos.</p>';
  } catch (e) {
    alert(e.message);
  }
}

// ------------------------------------------------------------------
// Tab: Recursos
// ------------------------------------------------------------------
async function cargarRecursos() {
  try {
    const r = await api("GET", "/recursos");
    document.querySelector("#tabla-hosts tbody").innerHTML = r.hosts.map(h => `<tr><td>${h.host}</td><td>${h.zona}</td>
      <td>${h.vcpu_usados}/${h.vcpu_total}</td><td>${h.ram_usada_mb}/${h.ram_total_mb}</td>
      <td>${h.disco_usado_gb}</td></tr>`).join("");
    document.querySelector("#tabla-mis-slices tbody").innerHTML = r.mis_slices.map(s => `<tr><td>${s.nombre}</td><td>${s.nodos}</td>
      <td>${s.vcpu}</td><td>${s.ram_mb}</td><td>${s.disco_gb}</td></tr>`).join("");
    document.getElementById("recursos-total").textContent =
      `Total propio: ${r.total_mio.vcpu} vCPU, ${r.total_mio.ram_mb} MB RAM, ${r.total_mio.disco_gb} GB disco.`;
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
  badge.textContent = activos.size ? `(${activos.size})` : "";
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
