import type { Config } from "tailwindcss";
import animate from "tailwindcss-animate";

// PaySilo palette — desert-dusk: deep teal ink, coral, clay, sage and sand.
//   #244855 ink · #E64833 coral · #874F41 clay · #90AEAD sage · #FBE9D0 sand
const config: Config = {
  darkMode: ["class"],
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    container: { center: true, padding: "1.25rem", screens: { "2xl": "1200px" } },
    extend: {
      fontFamily: {
        sans: ["var(--font-nunito)", "ui-rounded", "system-ui", "sans-serif"],
        display: ["Chillax", "var(--font-nunito)", "ui-rounded", "system-ui", "sans-serif"],
      },
      colors: {
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: { DEFAULT: "hsl(var(--primary))", foreground: "hsl(var(--primary-foreground))" },
        secondary: { DEFAULT: "hsl(var(--secondary))", foreground: "hsl(var(--secondary-foreground))" },
        muted: { DEFAULT: "hsl(var(--muted))", foreground: "hsl(var(--muted-foreground))" },
        accent: { DEFAULT: "hsl(var(--accent))", foreground: "hsl(var(--accent-foreground))" },
        destructive: { DEFAULT: "hsl(var(--destructive))", foreground: "hsl(var(--destructive-foreground))" },
        card: { DEFAULT: "hsl(var(--card))", foreground: "hsl(var(--card-foreground))" },
        popover: { DEFAULT: "hsl(var(--popover))", foreground: "hsl(var(--popover-foreground))" },
        ink: { 950: "#0C1D23", 900: "#11262E", 850: "#15303A", 800: "#1B3A45", 700: "#244855", 600: "#2F5A69", 500: "#3D6E7E" },
        coral: { 300: "#F2917F", 400: "#EC6A52", 500: "#E64833", 600: "#C93A27", 700: "#A42F1F" },
        clay: { 300: "#C08A7C", 400: "#A5675A", 500: "#874F41", 600: "#6E3F34", 700: "#563129" },
        sage: { 300: "#B9CFCE", 400: "#A3BFBE", 500: "#90AEAD", 600: "#6F8F8E", 700: "#557271" },
        sand: { 50: "#FFFAF3", 100: "#FFF4E4", 200: "#FBE9D0", 300: "#F3D7B0", 400: "#E5BF8C" },
        mint: { 400: "#7FD1AE", 500: "#5BBF97", 700: "#3A8C6B" },
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 4px)",
        sm: "calc(var(--radius) - 8px)",
        "4xl": "2rem",
      },
      boxShadow: {
        // Duolingo-style "3D" bottom edge; colour comes from --duo-edge
        duo: "0 5px 0 0 var(--duo-edge)",
        "duo-sm": "0 3px 0 0 var(--duo-edge)",
        glow: "0 0 90px -10px rgba(144, 174, 173, 0.45)",
        "glow-coral": "0 0 90px -10px rgba(230, 72, 51, 0.5)",
      },
      keyframes: {
        float: { "0%, 100%": { transform: "translateY(0)" }, "50%": { transform: "translateY(-10px)" } },
        marquee: { from: { transform: "translateX(0)" }, to: { transform: "translateX(-50%)" } },
        "otp-shake": { "0%, 100%": { transform: "translateX(0)" }, "20%, 60%": { transform: "translateX(-6px)" }, "40%, 80%": { transform: "translateX(6px)" } },
      },
      animation: {
        float: "float 5s ease-in-out infinite",
        "float-slow": "float 7s ease-in-out infinite",
        marquee: "marquee 30s linear infinite",
        "otp-shake": "otp-shake 0.4s ease-in-out",
      },
    },
  },
  plugins: [animate],
};

export default config;
