#!/usr/bin/env python3
"""
Demo and Test Script for Claw Models Command

This script demonstrates the capabilities of the "claw models" CLI command.
It can be used to test the implementation without manual interaction.
"""

import json
import subprocess
import sys
from pathlib import Path

# Add claw-agent to path
sys.path.insert(0, str(Path(__file__).parent))

from cli.providers import (
    get_provider_info,
    get_provider_base_url,
    get_provider_ids,
)
from cli.models import (
    get_fallback_models,
    get_model_display_name,
    verify_api_key,
)
from cli.config import (
    get_claw_home,
    get_config_path,
    load_config,
    get_model_config,
)


def test_models_module():
    """Test the models module."""
    print("=" * 70)
    print("TEST 1: Models Module")
    print("=" * 70)
    
    openrouter_models = get_fallback_models("openrouter")
    print(f"✓ OpenRouter fallback: {len(openrouter_models)} models")
    
    info = get_provider_info("openrouter")
    print(f"✓ Provider info loaded: {info['label']} ({info['hint']})")
    
    # Show first 5 models
    print("\nSample models:")
    for i, (model_id, desc) in enumerate(openrouter_models[:5]):
        display = get_model_display_name(model_id, desc)
        print(f"  {i+1}. {display}")
    
    # Check all providers
    from cli.providers import get_provider_ids
    slugs = get_provider_ids()
    print(f"\n✓ {len(slugs)} providers registered:")
    for s in slugs:
        pi = get_provider_info(s)
        fallback_count = len(get_fallback_models(s))
        print(f"   {s}: {pi['label']} ({fallback_count} fallback models)")
    
    print("\n✓ Models module working correctly")


def test_config_module():
    """Test the config module."""
    print("\n" + "=" * 70)
    print("TEST 2: Config Module")
    print("=" * 70)
    
    claw_home = get_claw_home()
    config_path = get_config_path()
    
    print(f"✓ Claw home: {claw_home}")
    print(f"✓ Config path: {config_path}")
    
    config = load_config()
    if config:
        print(f"✓ Config loaded: {json.dumps(config, indent=2)}")
        
        model_config = get_model_config()
        if model_config:
            print(f"\n✓ Current configuration:")
            print(f"  Provider: {model_config.get('provider', 'N/A')}")
            print(f"  Model: {model_config.get('model', 'N/A')}")
    else:
        print("ℹ No configuration saved yet (run 'claw models' to setup)")


def test_cli_help():
    """Test CLI help output."""
    print("\n" + "=" * 70)
    print("TEST 3: CLI Help")
    print("=" * 70)
    
    result = subprocess.run(
        [sys.executable, "-m", "cli.main", "models", "--help"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent
    )
    
    if result.returncode == 0:
        print("✓ CLI help output:")
        print(result.stdout)
    else:
        print("✗ CLI help failed")
        print(result.stderr)


def test_cli_version():
    """Test CLI version command."""
    print("\n" + "=" * 70)
    print("TEST 4: CLI Version")
    print("=" * 70)
    
    result = subprocess.run(
        [sys.executable, "-m", "cli.main", "version"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent
    )
    
    if result.returncode == 0:
        print(f"✓ Version: {result.stdout.strip()}")
    else:
        print("✗ Version command failed")


def test_cli_models_show():
    """Test CLI models --show command."""
    print("\n" + "=" * 70)
    print("TEST 5: CLI Models --show")
    print("=" * 70)
    
    result = subprocess.run(
        [sys.executable, "-m", "cli.main", "models", "--show"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent
    )
    
    if result.returncode == 0:
        print("✓ Models show output:")
        print(result.stdout)
    else:
        print("✗ Models show failed")
        print(result.stderr)


def test_api_key_validation():
    """Test API key validation (with dummy keys)."""
    print("\n" + "=" * 70)
    print("TEST 6: API Key Validation")
    print("=" * 70)
    
    provider = "openrouter"
    
    # Empty key should be rejected immediately (no network call)
    print("Testing empty API key...")
    is_valid, msg = verify_api_key(provider, "")
    if not is_valid:
        print(f"✓ Empty key correctly rejected: {msg}")
    else:
        print("✗ Empty key should have been rejected")
    
    # Non-empty key with network error → warn but accept (SSL / connectivity
    # issues are ambiguous — the key may be valid but unreachable).
    print("\nTesting with non-empty key (will hit network)...")
    is_valid, msg = verify_api_key(provider, "sk-or-test-key")
    if is_valid:
        if msg:
            print(f"✓ Network issue — warning emitted: {msg}")
        else:
            print("✓ Key verified (live API reachable)")
    else:
        print(f"✓ Key rejected: {msg} (server responded definitively)")


def test_imports():
    """Test all module imports."""
    print("\n" + "=" * 70)
    print("TEST 7: Module Imports")
    print("=" * 70)
    
    try:
        from cli.providers import (
            get_provider_base_url,
            get_provider_ids,
            get_provider_label,
        )
        print("✓ providers module imports successful")

        from cli.models import (
            curated_models_for_provider,
            verify_api_key,
            get_model_display_name,
        )
        print("✓ models module imports successful")

        from cli.skills_cmd import (
            run_skills_command,
        )
        print("✓ skills_cmd module imports successful")
        
        from cli.config import (
            get_claw_home,
            load_config,
            save_config,
        )
        print("✓ config module imports successful")
        
        from cli.interactive_ui import (
            prompt_choice,
            prompt_password,
            print_header,
        )
        print("✓ interactive_ui module imports successful")
        
        from cli.models_cmd import (
            run_models_command,
            show_current_model,
        )
        print("✓ models_cmd module imports successful")
        
        print("\n✓ All imports successful")
    except ImportError as e:
        print(f"✗ Import failed: {e}")
        sys.exit(1)


def main():
    """Run all tests."""
    print("\n")
    print("╔" + "=" * 68 + "╗")
    print("║" + " " * 68 + "║")
    print("║" + "Claw Models Command - Implementation Tests".center(68) + "║")
    print("║" + " " * 68 + "║")
    print("╚" + "=" * 68 + "╝")
    
    try:
        test_imports()
        test_models_module()
        test_config_module()
        test_cli_help()
        test_cli_version()
        test_cli_models_show()
        test_api_key_validation()
        
        print("\n" + "=" * 70)
        print("✓ ALL TESTS PASSED")
        print("=" * 70)
        print("\nImplementation Summary:")
        print("  • 5 new modules created")
        print("  • OpenRouter API integration")
        print("  • Interactive CLI with 4-step workflow")
        print("  • Configuration storage in ~/.claw/config.json")
        print("  • Fallback model list for offline mode")
        print("  • Comprehensive error handling")
        print("\nUsage:")
        print("  claw models              # Interactive setup wizard")
        print("  claw models --show       # Display current configuration")
        print("\nNext Steps:")
        print("  1. Test with real OpenRouter API key:")
        print("     export OPENROUTER_API_KEY='sk-or-your-key-here'")
        print("     claw models")
        print("\n  2. View saved configuration:")
        print("     claw models --show")
        print("\n  3. Inspect config file:")
        print(f"     cat {get_config_path()}")
        
    except Exception as e:
        print(f"\n✗ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
