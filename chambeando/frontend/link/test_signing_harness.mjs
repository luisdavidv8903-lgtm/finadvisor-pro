// Arnes de prueba (revision 2026-09-25, hallazgo de revision) -- NO es
// parte de la pagina servida (nada en index.html lo referencia). Extrae y
// EJECUTA el <script> inline real de index.html dentro de un DOM/window
// minimo, con window.tronWeb/window.ethereum simulados, para probar que la
// pagina realmente usa el metodo de firma correcto por red -- no solo que
// el texto fuente lo menciona en algun lado. Los mocks de tronWeb.trx.sign
// (el metodo deprecado) y de cualquier metodo inesperado ARROJAN si se
// llaman, asi que un regreso accidental al metodo viejo hace fallar el
// arnes de inmediato.
//
// Uso: node test_signing_harness.mjs <network>
// Imprime un JSON de una linea con lo que paso; codigo de salida 0 si el
// arnes corrio sin excepciones no manejadas (el llamador de Python decide
// que campos del JSON validar).
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const network = process.argv[2];
if (!network) {
  console.error("uso: node test_signing_harness.mjs <network>");
  process.exit(2);
}

const html = readFileSync(path.join(__dirname, "index.html"), "utf8");
const inlineScripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (inlineScripts.length !== 1) {
  console.error(`se esperaba exactamente 1 <script> inline en index.html, se encontraron ${inlineScripts.length}`);
  process.exit(2);
}
const pageScript = inlineScripts[0];

const calls = [];

function makeElement(id) {
  // connectBtn arranca disabled en el HTML real (<button ... disabled>) --
  // el script solo lo habilita explicitamente en las ramas tron/evm.
  const el = { id, textContent: "", className: "", style: {}, disabled: id === "connectBtn", _listeners: {} };
  el.addEventListener = (evt, fn) => {
    el._listeners[evt] = fn;
  };
  el.remove = () => {};
  return el;
}
const elements = { intro: makeElement("intro"), connectBtn: makeElement("connectBtn"), status: makeElement("status"), unverifiedNote: makeElement("unverifiedNote") };

const tronWebMock = {
  defaultAddress: { base58: "TMockTronAddress00000000000001" },
  trx: {
    signMessageV2: async (message) => {
      calls.push(["tronWeb.trx.signMessageV2", message]);
      return "0xmocktronsig";
    },
    sign: async () => {
      throw new Error("REGRESION: se llamo al metodo DEPRECADO tronWeb.trx.sign() -- debe usarse signMessageV2");
    },
  },
};
const tronLinkMock = {
  request: async ({ method }) => {
    calls.push(["tronLink.request", method]);
    return { code: 200 };
  },
};

const ethereumMock = {
  request: async ({ method, params }) => {
    calls.push([`ethereum.request:${method}`, params]);
    if (method === "eth_requestAccounts") return ["0xMockEvmAddress0000000000000000000000001"];
    if (method === "personal_sign") return "0xmockevmsig";
    throw new Error(`REGRESION: metodo EVM inesperado llamado: ${method}`);
  },
};

const fakeFetch = async (url) => {
  calls.push(["fetch", url]);
  return {
    ok: true,
    json: async () => ({
      message: "mock message to sign",
      access_token: "mock-jwt",
      whatsapp_id: "wa-mock",
      linked: true,
    }),
  };
};

const sandbox = {
  console,
  document: { getElementById: (id) => elements[id] || makeElement(id) },
  window: {
    location: { search: `?token=mocktoken&network=${network}`, pathname: "/link" },
    history: { replaceState: () => {} },
    CHAMBEANDO_LINK_CONFIG: { apiBaseUrl: "https://mock-api.example" },
    ethereum: network === "evm" ? ethereumMock : undefined,
    tronWeb: network === "tron" ? tronWebMock : undefined,
    tronLink: network === "tron" ? tronLinkMock : undefined,
  },
  URL,
  URLSearchParams,
  TextEncoder,
  fetch: fakeFetch,
};

let loadError = null;
vm.createContext(sandbox);
try {
  vm.runInContext(pageScript, sandbox, { filename: "index.html-inline-script" });
} catch (err) {
  loadError = String(err);
}

async function main() {
  const output = {
    network,
    loadError,
    btnDisabledAfterLoad: elements.connectBtn.disabled,
    introTextAfterLoad: elements.intro.textContent,
  };

  if (loadError || elements.connectBtn.disabled) {
    console.log(JSON.stringify({ ...output, calls, clicked: false }));
    return;
  }

  const clickHandler = elements.connectBtn._listeners.click;
  let clickError = null;
  if (clickHandler) {
    try {
      await clickHandler();
    } catch (err) {
      clickError = String(err);
    }
  }

  console.log(
    JSON.stringify({
      ...output,
      clicked: true,
      clickError,
      calls,
      finalStatusText: elements.status.textContent,
    })
  );
}

main();
