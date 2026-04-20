import os
import re

def fix_templates():
    endpoints = [
        'manage_items',
        'edit_saved_item',
        'update_saved_item',
        'remove_saved_item',
        'move_saved_item',
        'restore_saved_item',
        'bulk_update_items'
    ]
    
    count = 0
    for root, _, files in os.walk('templates'):
        for file in files:
            if not file.endswith('.html'): continue
            
            path = os.path.join(root, file)
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
                
            new_content = content
            for ep in endpoints:
                # Matches url_for('manage_items' or url_for("manage_items" and safely prefixes items.
                new_content = re.sub(
                    rf"(url_for\(\s*['\"]){ep}(['\"])", 
                    rf"\g<1>items.{ep}\g<2>", 
                    new_content
                )
                
            if new_content != content:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                print(f"Fixed updated routes in: {path}")
                count += 1
                
    print(f"Done! {count} template(s) updated.")

if __name__ == '__main__':
    fix_templates()