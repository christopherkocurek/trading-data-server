#!/usr/bin/env python3
"""
Sync Trading Expert Skill to Railway Agent

Reads the skill context files and generates an updated TRADING_EXPERT_SYSTEM
prompt for the trading agent.

Usage:
    python sync_skill.py              # Preview the generated prompt
    python sync_skill.py --apply      # Update trading_agent.py and push
    python sync_skill.py --apply --no-push  # Update without pushing
"""

import os
import re
import argparse
import subprocess
from pathlib import Path

# Paths
SKILL_DIR = Path.home() / ".claude" / "skills" / "trading-expert" / "context"
AGENT_FILE = Path(__file__).parent / "trading_agent.py"

# Files to include and their key sections to extract
CONTEXT_FILES = {
    "market-analysis.md": {
        "name": "ANALYSIS FRAMEWORK",
        "sections": ["Step 1:", "Step 2:", "Step 3:", "Quick Reference"]
    },
    "risk-framework.md": {
        "name": "RISK FRAMEWORK",
        "sections": ["Core Position Sizing", "Kelly Criterion", "Stop-Loss Rules", "Portfolio Heat", "Drawdown Response", "Black Swan"]
    },
    "entry-rules.md": {
        "name": "ENTRY SIGNALS",
        "sections": ["Hash Ribbon", "Funding Rate Squeeze", "Wyckoff Spring", "Extreme Fear"]
    },
    "exit-rules.md": {
        "name": "EXIT SIGNALS",
        "sections": ["MVRV >", "NUPL >", "Extreme Greed", "LTH Distribution", "Emergency"]
    },
    "btc-specific.md": {
        "name": "BTC-SPECIFIC",
        "sections": ["Halving Cycle", "Mining-Based", "Dominance", "Weekend", "ETF Flow"]
    },
    "mental-models.md": {
        "name": "MENTAL MODELS",
        "sections": ["Fear & Greed", "Capitulation", "PTJ:", "Druckenmiller:", "Livermore:", "Minervini:"]
    }
}


def read_file(filepath: Path) -> str:
    """Read file contents."""
    if filepath.exists():
        return filepath.read_text()
    return ""


def extract_key_content(content: str, sections: list) -> str:
    """Extract key sections from content."""
    lines = content.split('\n')
    extracted = []
    capturing = False
    capture_depth = 0

    for line in lines:
        # Check if this line starts a section we want
        for section in sections:
            if section.lower() in line.lower():
                capturing = True
                capture_depth = line.count('#')
                extracted.append(line)
                break
        else:
            if capturing:
                # Stop capturing if we hit a header of same or higher level
                current_depth = len(re.match(r'^#+', line).group()) if re.match(r'^#+', line) else 0
                if current_depth > 0 and current_depth <= capture_depth and not any(s.lower() in line.lower() for s in sections):
                    capturing = False
                else:
                    extracted.append(line)

    return '\n'.join(extracted)


def condense_content(content: str, max_lines: int = 50) -> str:
    """Condense content to fit within limits."""
    lines = content.split('\n')

    # Remove empty lines and very long lines
    condensed = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Skip code blocks
        if line.startswith('```'):
            continue
        # Truncate very long lines
        if len(line) > 200:
            line = line[:200] + "..."
        condensed.append(line)

    # If still too long, take most important (headers + tables + key points)
    if len(condensed) > max_lines:
        priority = []
        for line in condensed:
            if line.startswith('#') or line.startswith('|') or line.startswith('-') or line.startswith('*'):
                priority.append(line)
        condensed = priority[:max_lines]

    return '\n'.join(condensed)


def generate_system_prompt() -> str:
    """Generate the TRADING_EXPERT_SYSTEM prompt from skill files."""

    sections = []

    sections.append("""You are an elite BTC trading analyst with deep expertise in macro, on-chain, derivatives, and technical analysis. You follow a systematic 4-step framework and legendary trader principles.""")

    for filename, config in CONTEXT_FILES.items():
        filepath = SKILL_DIR / filename
        content = read_file(filepath)

        if not content:
            print(f"Warning: {filename} not found")
            continue

        # Extract relevant sections
        extracted = extract_key_content(content, config["sections"])
        condensed = condense_content(extracted, max_lines=40)

        if condensed:
            sections.append(f"\n## {config['name']}\n{condensed}")

    # Add output format
    sections.append("""
## OUTPUT FORMAT
Be direct and conversational like a senior trader briefing a colleague. Cover:
1. Current situation (price action, key levels, sentiment)
2. Framework assessment (which step is dominant right now)
3. Key levels to watch (support/resistance, liquidation clusters)
4. Actionable bias with confidence level

End with: **Bias: [BULLISH/BEARISH/NEUTRAL]** | Confidence: X/10""")

    return '\n'.join(sections)


def update_agent_file(new_prompt: str) -> bool:
    """Update the TRADING_EXPERT_SYSTEM in trading_agent.py."""
    content = AGENT_FILE.read_text()

    # Find and replace the TRADING_EXPERT_SYSTEM constant
    pattern = r'TRADING_EXPERT_SYSTEM = """.*?"""'
    replacement = f'TRADING_EXPERT_SYSTEM = """{new_prompt}"""'

    new_content, count = re.subn(pattern, replacement, content, flags=re.DOTALL)

    if count == 0:
        print("Error: Could not find TRADING_EXPERT_SYSTEM in trading_agent.py")
        return False

    AGENT_FILE.write_text(new_content)
    print(f"✓ Updated {AGENT_FILE}")
    return True


def git_push():
    """Commit and push changes."""
    os.chdir(AGENT_FILE.parent)

    subprocess.run(["git", "add", "trading_agent.py"], check=True)
    subprocess.run([
        "git", "commit", "-m",
        "Sync trading expert skill knowledge\n\nAuto-generated from ~/.claude/skills/trading-expert/context/\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
    ], check=True)
    subprocess.run(["git", "push"], check=True)
    print("✓ Pushed to GitHub - Railway will auto-deploy")


def main():
    parser = argparse.ArgumentParser(description="Sync trading skill to Railway agent")
    parser.add_argument("--apply", action="store_true", help="Apply changes to trading_agent.py")
    parser.add_argument("--no-push", action="store_true", help="Don't push to GitHub after applying")
    args = parser.parse_args()

    print("=" * 60)
    print("Trading Expert Skill Sync")
    print("=" * 60)
    print(f"\nSkill directory: {SKILL_DIR}")
    print(f"Agent file: {AGENT_FILE}")

    # Check skill directory exists
    if not SKILL_DIR.exists():
        print(f"\nError: Skill directory not found: {SKILL_DIR}")
        return 1

    # Generate the prompt
    print("\nReading skill context files...")
    prompt = generate_system_prompt()

    print(f"\nGenerated prompt ({len(prompt)} chars):")
    print("-" * 60)
    print(prompt[:2000] + "..." if len(prompt) > 2000 else prompt)
    print("-" * 60)

    if args.apply:
        print("\nApplying changes...")
        if update_agent_file(prompt):
            if not args.no_push:
                print("\nPushing to GitHub...")
                try:
                    git_push()
                except subprocess.CalledProcessError as e:
                    print(f"Git error: {e}")
                    return 1
            else:
                print("\nSkipping push (--no-push specified)")
                print("Run manually: cd ~/trading-expert-research/server && git add -A && git commit -m 'Update skill' && git push")
    else:
        print("\nPreview only. Run with --apply to update trading_agent.py")

    return 0


if __name__ == "__main__":
    exit(main())
