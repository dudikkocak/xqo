// Builds the editor into ../src/xqo_flows/static/ (editor.js + editor.css). Run: npm ci && npm run build
import { build } from "esbuild";
await build({
  entryPoints: ["src/main.tsx"],
  bundle: true,
  minify: true,
  format: "iife",
  target: ["es2020"],
  outfile: "../src/xqo_flows/static/editor.js",
  define: { "process.env.NODE_ENV": '"production"' },
  loader: { ".svg": "dataurl", ".png": "dataurl" },
  legalComments: "eof",
  logLevel: "info",
});
