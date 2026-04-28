/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        critical: "#dc2626",
        significant: "#ea580c",
        minor: "#ca8a04",
        uncertain: "#6b7280",
      },
    },
  },
  plugins: [],
};
