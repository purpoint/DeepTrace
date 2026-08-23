/** @type {import('tailwindcss').Config} */
export default {
  // Class-based rather than media-based: the user's choice has to be able to
  // disagree with their operating system. A site that can only follow the OS
  // has a preference, not a setting.
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // The interface is drawn from CSS variables so one set of class names
        // serves both themes. A component written with `dark:` on every colour
        // is a component maintained twice, and the second one drifts.
        ink: "rgb(var(--ink) / <alpha-value>)",
        muted: "rgb(var(--muted) / <alpha-value>)",
        faint: "rgb(var(--faint) / <alpha-value>)",
        canvas: "rgb(var(--canvas) / <alpha-value>)",
        surface: "rgb(var(--surface) / <alpha-value>)",
        raised: "rgb(var(--raised) / <alpha-value>)",
        line: "rgb(var(--line) / <alpha-value>)",

        // The signature colour. Cyan because every other colour in this
        // interface already means something -- green is supported, amber is
        // partial, rose is unsupported, violet is disputed -- and a brand that
        // collides with a verdict makes the verdict harder to read.
        brand: {
          DEFAULT: "rgb(var(--brand) / <alpha-value>)",
          soft: "rgb(var(--brand-soft) / <alpha-value>)",
        },

        verdict: {
          supported: "rgb(var(--supported) / <alpha-value>)",
          partial: "rgb(var(--partial) / <alpha-value>)",
          unsupported: "rgb(var(--unsupported) / <alpha-value>)",
          conflicting: "rgb(var(--conflicting) / <alpha-value>)",
        },
      },
      fontFamily: {
        sans: ["Inter var", "Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      keyframes: {
        "fade-up": {
          from: { opacity: "0", transform: "translateY(12px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "slide-in": {
          from: { opacity: "0", transform: "translateY(6px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "flash": {
          "0%, 100%": { backgroundColor: "transparent" },
          "20%": { backgroundColor: "rgb(var(--brand) / 0.16)" },
        },
        "pulse-ring": {
          "0%": { boxShadow: "0 0 0 0 rgb(var(--brand) / 0.5)" },
          "70%": { boxShadow: "0 0 0 8px rgb(var(--brand) / 0)" },
          "100%": { boxShadow: "0 0 0 0 rgb(var(--brand) / 0)" },
        },
      },
      animation: {
        "fade-up": "fade-up 0.5s cubic-bezier(0.16, 1, 0.3, 1) both",
        "slide-in": "slide-in 0.28s cubic-bezier(0.16, 1, 0.3, 1) both",
        flash: "flash 1.6s ease-out",
        "pulse-ring": "pulse-ring 2s ease-out infinite",
      },
    },
  },
  plugins: [],
};
