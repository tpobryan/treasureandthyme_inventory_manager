import os
import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from inventory_manager_generator import InventoryManagerGenerator

def test_one_option():
    # Use a dummy key if not set, but we want to hit the real API if possible
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or api_key == "your_openai_key":
        print("Error: Valid OPENAI_API_KEY environment variable required for real test.")
        return

    gen = InventoryManagerGenerator(api_key=api_key)
    
    print("Testing 'High Certainty' scenario with explicit seller notes...")
    # We'll provide a very specific note that should trigger high confidence
    seller_notes = "This is a 1950s Omega Seamaster Automatic watch, stainless steel, reference 2846, with a honeycomb dial. 100% authentic."
    
    # We need a placeholder path, the generator just needs the paths to exist usually 
    # but the build_image_content will try to read them. 
    # For this test, I will mock the build_image_content internally to avoid needing a real file.
    
    try:
        # Mocking build_image_content for a text-only certainty test
        import inventory_manager_generator
        original_build = inventory_manager_generator._build_image_content
        inventory_manager_generator._build_image_content = lambda x: [] 
        
        result = gen.generate_options(image_paths=[Path("dummy.jpg")], seller_notes=seller_notes)
        
        options = result.get("options", [])
        print(f"\nNumber of options returned: {len(options)}")
        for i, opt in enumerate(options):
            print(f"Option {i+1} Title: {opt.get('title')}")
            
        if len(options) == 1:
            print("\nSUCCESS: Logic correctly returned only ONE option for a high-certainty item.")
        else:
            print(f"\nNOTE: Returned {len(options)} options. If the AI still sent 3, the instructions may need further weighting.")
            
        inventory_manager_generator._build_image_content = original_build
    except Exception as e:
        print(f"Error during test: {e}")

if __name__ == "__main__":
    test_one_option()
