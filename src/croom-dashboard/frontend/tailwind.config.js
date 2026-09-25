/** @type {import('tailwindcss').Config} */
// Crystal PM accent on Tailwind's stock charcoal grays. Only the blue scale is
// remapped to brand shades; gray stays default (#111827 page, #1F2937 cards).
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Lexend', 'Segoe UI', 'Arial', 'sans-serif'],
      },
      colors: {
        blue: {
          50: '#F7F9FF',
          100: '#EDF2FE',
          200: '#DCE5FF',
          300: '#BDCEFF',
          400: '#6C92F5',
          500: '#3B6DF0',
          600: '#1B52E5',
          700: '#003EBC',
          800: '#00308F',
          900: '#001F5C',
        },
      },
    },
  },
  plugins: [],
}
