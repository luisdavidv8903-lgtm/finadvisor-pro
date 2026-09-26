// Configuracion del pagina /link -- archivo SEPARADO de index.html a
// proposito (revision 2026-09-25, correccion: "elimina localhost de
// cualquier configuracion prevista para HTTPS real, sin permitir que el
// query string elija la API"). index.html NUNCA lee la URL de la API desde
// location.search/location.hash -- SOLO desde este archivo.
//
// Este archivo, tal como esta ahora, trae el valor de DESARROLLO LOCAL
// (apiBaseUrl=http://localhost:8000). Es el UNICO que index.html carga
// (<script src="./config.js">) -- no hay un config.js "de produccion"
// separado conviviendo en el repo, porque no hay ningun deploy real
// todavia (ver bundle de revision: "no despliegues el frontend"). Un
// deploy real DEBE reemplazar el contenido de ESTE archivo por la URL
// HTTPS real del backend antes de servir la pagina -- index.html se niega
// a arrancar (ver su chequeo de apiBaseUrl) si el valor no es "https://..."
// ni "http://localhost..." explicitamente, para que un despliegue que se
// olvide de cambiar esto falle ruidosamente en vez de operar contra un
// origen equivocado o inseguro.
window.CHAMBEANDO_LINK_CONFIG = {
  apiBaseUrl: "http://localhost:8000",
};
