import { defineConfig } from "vite";

export default defineConfig({
  server: { port: 42380, strictPort: false },
  preview: { port: 42390, strictPort: false },
  base: "/app/micast/",
  build: {
    outDir: "dist",
    assetsDir: "assets",
    emptyOutDir: true,
    rollupOptions: {
      output: {
        entryFileNames: "assets/[name]-[hash].js",
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
  },
});
