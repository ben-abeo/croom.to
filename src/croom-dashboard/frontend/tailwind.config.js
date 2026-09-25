/** @type {import('tailwindcss').Config} */
// Crystal PM blue theme. The gray and blue scales are remapped to brand shades so
// every page inherits the palette without editing each class.
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
        gray: {
          50: '#F7F9FF',
          100: '#EDF2FE',
          200: '#DCE5FF',
          300: '#BDCEFF',
          400: '#8E9AB1',
          500: '#647087',
          600: '#33477F',
          700: '#22346B',
          800: '#16244F',
          900: '#001636',
        },
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
