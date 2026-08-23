/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Named by role rather than by hue, so a claim's status has one colour
        // wherever it appears. A verdict rendered green in one view and grey in
        // another reads as two different verdicts.
        supported: "#15803d",
        partial: "#b45309",
        unsupported: "#b91c1c",
        conflicting: "#7c3aed",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "monospace"],
      },
    },
  },
  plugins: [],
};
