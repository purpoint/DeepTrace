/// <reference types="vite/client" />

/** Build-time configuration this client reads.
 *
 *  One variable, and it is optional. The default -- empty -- is the single
 *  origin arrangement the compose stack serves: nginx hands out this client
 *  and proxies /api beside it. `VITE_API_ORIGIN` exists only for a split
 *  deployment, where the client is on a static host and the API is somewhere
 *  else, and it is the point at which CORS becomes load-bearing.
 */
interface ImportMetaEnv {
  readonly VITE_API_ORIGIN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
