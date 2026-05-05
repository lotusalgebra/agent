/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        canvas: '#0a0f1c',
        surface: '#141a28',
        surface2: '#1a2134',
        border: '#232d45',
        'border-soft': '#1a2238',
        text: '#e6ebf5',
        'text-dim': '#7f8aa3',
        muted: '#4e5872',
        accent: '#ff6b35',
        'accent-strong': '#ff8458',
        green: '#10b981',
        amber: '#fbbf24',
        cyan: '#00e5ff',
        violet: '#a78bfa',
      },
      fontFamily: {
        sans: ['system-ui', '-apple-system', 'Helvetica Neue', 'sans-serif'],
        display: ['Orbitron', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'monospace'],
      },
      animation: {
        'wire-flow': 'wire-flow 0.8s linear infinite',
        'pulse-soft': 'pulse-soft 2s cubic-bezier(0.4,0,0.6,1) infinite',
        'slide-down': 'slide-down 0.3s cubic-bezier(0.2,0.8,0.3,1)',
        shake: 'shake 0.45s cubic-bezier(0.36,0.07,0.19,0.97)',
      },
      keyframes: {
        'wire-flow': { to: { 'stroke-dashoffset': '-22' } },
        'pulse-soft': { '0%,100%': { opacity: '1' }, '50%': { opacity: '0.6' } },
        'slide-down': {
          from: { transform: 'translateY(-12px)', opacity: '0' },
          to:   { transform: 'translateY(0)', opacity: '1' },
        },
        shake: {
          '10%,90%':      { transform: 'translateX(-1px)' },
          '20%,80%':      { transform: 'translateX(2px)' },
          '30%,50%,70%':  { transform: 'translateX(-4px)' },
          '40%,60%':      { transform: 'translateX(4px)' },
        },
      },
    },
  },
  plugins: [],
}
