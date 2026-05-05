#!/usr/bin/env bash
# LOTUS Agent — Phase 1 Auto-Setup
# Run with: bash setup_phase1.sh
# This installs everything needed for Phase 1 on macOS.

set -e  # Exit on any error

echo ""
echo "════════════════════════════════════════════════════"
echo "    LOTUS AGENT — Phase 1 Setup"
echo "    Ollama + Gemma 4 E4B + Python Voice Engine"
echo "════════════════════════════════════════════════════"
echo ""

# ═══ Check Prerequisites ═══

echo "🔍 Checking Mac environment..."
sw_vers | head -2
echo ""
echo "💻 Hardware:"
system_profiler SPHardwareDataType | grep -E "Chip|Memory"
echo ""
echo "🐍 Python:"
python3 --version
echo ""

read -p "Continue with setup? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    exit 1
fi

# ═══ Homebrew ═══

if ! command -v brew &> /dev/null; then
    echo "📦 Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

    # Add to shell profile
    echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
    eval "$(/opt/homebrew/bin/brew shellenv)"
else
    echo "✅ Homebrew already installed"
fi

# ═══ Ollama ═══

if ! command -v ollama &> /dev/null; then
    echo "📦 Installing Ollama..."
    brew install ollama
else
    echo "✅ Ollama already installed"
fi

echo "🚀 Starting Ollama service..."
brew services start ollama || true
sleep 3

# ═══ Gemma 4 E4B ═══

echo "📥 Pulling Gemma 4 E4B model (~5GB, may take 5-10 min)..."
ollama pull gemma4:e4b

echo ""
echo "🧪 Testing Gemma..."
echo "Say hello" | ollama run gemma4:e4b --verbose=false || true

# ═══ Portaudio for mic ═══

echo "🎤 Installing audio dependencies..."
brew install portaudio

# ═══ Python virtual environment ═══

echo ""
echo "🐍 Setting up Python virtual environment..."
cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

source venv/bin/activate

echo "📦 Installing Python packages..."
pip install --upgrade pip
pip install -r requirements_phase1.txt

# ═══ Tests ═══

echo ""
echo "════════════════════════════════════════════════════"
echo "  🧪 RUNNING TESTS"
echo "════════════════════════════════════════════════════"
echo ""

echo "Test 1: Ollama API connection..."
python3 -c "
import requests
r = requests.get('http://localhost:11434/api/tags')
models = [m['name'] for m in r.json().get('models', [])]
print(f'   Models available: {models}')
assert any('gemma4' in m for m in models), 'Gemma 4 not found!'
print('   ✅ Ollama + Gemma 4 E4B working')
"

echo ""
echo "Test 2: Gemma 4 inference..."
python3 -c "
import requests
r = requests.post('http://localhost:11434/api/generate',
                  json={'model': 'gemma4:e4b',
                        'prompt': 'Say hello in one sentence',
                        'stream': False})
print(f'   Gemma says: {r.json()[\"response\"][:100]}')
print('   ✅ Inference working')
"

echo ""
echo "════════════════════════════════════════════════════"
echo "  ✅ PHASE 1 SETUP COMPLETE"
echo "════════════════════════════════════════════════════"
echo ""
echo "Next steps:"
echo ""
echo "1. Test voice engine (speaks and listens):"
echo "   source venv/bin/activate"
echo "   python agent_phase1.py t"
echo ""
echo "2. Launch voice mode:"
echo "   python agent_phase1.py v"
echo ""
echo "3. Grant microphone permission when macOS asks:"
echo "   System Settings → Privacy & Security → Microphone"
echo ""
echo "Say 'Lotus, hello' to activate."
echo ""
