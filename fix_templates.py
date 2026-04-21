import os
import re

def fix_templates():
    endpoints = {
        'manage_items': 'items.manage_items',
        'edit_saved_item': 'items.edit_saved_item',
        'update_saved_item': 'items.update_saved_item',
        'remove_saved_item': 'items.remove_saved_item',
        'move_saved_item': 'items.move_saved_item',
        'restore_saved_item': 'items.restore_saved_item',
        'bulk_update_items': 'items.bulk_update_items',
        'auctions_overview': 'auctions.auctions_overview',
        'create_auction_route': 'auctions.create_auction_route',
        'switch_auction_route': 'auctions.switch_auction_route',
        'update_auction_status_route': 'auctions.update_auction_status_route',
        'export_csv': 'exports.export_csv',
        'export_history': 'exports.export_history',
        'export_batch_details': 'exports.export_batch_details',
        'download_export_archive': 'exports.download_export_archive',
        'export_selected_csv': 'exports.export_selected_csv',
        'index': 'main.index',
        'dashboard': 'main.dashboard',
        'analyze': 'main.analyze',
        'choose_option': 'main.choose_option',
        'add_draft_photos': 'main.add_draft_photos',
        'reorder_draft_photos': 'main.reorder_draft_photos',
        'remove_draft_photo': 'main.remove_draft_photo',
        'revise': 'main.revise',
        'save': 'main.save',
        'uploaded_file': 'main.uploaded_file',
        'set_next_lot': 'main.set_next_lot',
        'reset': 'main.reset',
        'resume_draft': 'main.resume_draft',
        'discard_draft': 'main.discard_draft',
        'admin': 'admin.admin',
        'delete_remote_upload': 'admin.delete_remote_upload',
        'upload_remote_ftp': 'admin.upload_remote_ftp',
        'ftp_preview': 'admin.ftp_preview',
        'upload_selected_ftp': 'admin.upload_selected_ftp',
        'login': 'auth.login',
        'logout': 'auth.logout',
    }
    
    count = 0
    for root, _, files in os.walk('templates'):
        for file in files:
            if not file.endswith('.html'): continue
            
            path = os.path.join(root, file)
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
                
            new_content = content
            for old_ep, new_ep in endpoints.items():
                # Matches url_for('old_ep' or url_for("old_ep" and replaces with new_ep
                new_content = re.sub(
                    rf"(url_for\(\s*['\"]){old_ep}(['\"])", 
                    rf"\g<1>{new_ep}\g<2>", 
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