/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        gdf: {
          bg:      "#0f1117",
          surface: "#171a23",
          panel:   "#1c202b",
          border:  "#2a2f3d",
          text:    "#e6e8ee",
          muted:   "#8a92a6",
          // Cool light-slate accent — used for focus rings, amounts column,
          // loading pulse, and inline links. Keeps the UI monochrome so the
          // teal logo is the only chromatic element.
          accent:  "#cbd5e1",
          // Teal matches the logo wordmark — used for emphasized numbers
          // (stat cards, amounts) and labels in the expanded row details.
          teal:    "#06b6d4",
        },
      },
      fontFamily: {
        sans: ['"Inter"', "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "SFMono-Regular", "monospace"],
      },
    },
  },
  plugins: [],
};
