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

        /* The modal. Scale and lift together so the card reads as coming
           toward the reader rather than sliding in from somewhere -- it is a
           thing being handed over, not a panel arriving. The overshoot easing
           does the rest. */
        "card-in": {
          from: { opacity: "0", transform: "translateY(16px) scale(0.94)" },
          to: { opacity: "1", transform: "translateY(0) scale(1)" },
        },
        /* Faster out than in, and without the overshoot. Dismissal should feel
           like the interface getting out of the way; a leisurely exit reads as
           the application arguing about it. */
        "card-out": {
          from: { opacity: "1", transform: "translateY(0) scale(1)" },
          to: { opacity: "0", transform: "translateY(8px) scale(0.97)" },
        },
        "backdrop-in": {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
        "backdrop-out": {
          from: { opacity: "1" },
          to: { opacity: "0" },
        },
        /* The bloom behind the card. Breathing rather than pulsing: slow
           enough that it reads as depth and not as something demanding
           attention. */
        /* A rule that draws itself left to right. */
        "rule": {
          from: { transform: "scaleX(0)" },
          to: { transform: "scaleX(1)" },
        },
        "bloom": {
          "0%, 100%": { opacity: "0.5", transform: "scale(1)" },
          "50%": { opacity: "0.8", transform: "scale(1.06)" },
        },
      },
      animation: {
        "fade-up": "fade-up 0.5s cubic-bezier(0.16, 1, 0.3, 1) both",
        "slide-in": "slide-in 0.28s cubic-bezier(0.16, 1, 0.3, 1) both",
        flash: "flash 1.6s ease-out",
        "pulse-ring": "pulse-ring 2s ease-out infinite",
        "card-in": "card-in 0.34s cubic-bezier(0.16, 1, 0.3, 1) both",
        "card-out": "card-out 0.16s ease-in both",
        "backdrop-in": "backdrop-in 0.22s ease-out both",
        "backdrop-out": "backdrop-out 0.16s ease-in both",
        rule: "rule 0.5s cubic-bezier(0.16, 1, 0.3, 1) 0.22s both",
        bloom: "bloom 6s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
