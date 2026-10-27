import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Satoshi", "ui-sans-system", "system-ui", "sans-serif"],
        display: ["'Clash Display'", "Satoshi", "system-ui", "sans-serif"],
      },
      colors: {
        ink: "#0a0a0b",
        panel: "#121214",
        line: "#232326",
      },
    },
  },
  plugins: [],
};

export default config;
