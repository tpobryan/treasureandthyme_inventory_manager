import logging
from database import ensure_item_store_ready, connect_item_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def run_migration():
    logger.info("Initializing database schema...")
    # This automatically adds new tables and alters existing tables to add columns
    ensure_item_store_ready()
    
    logger.info("Connecting to item store to perform data backfills...")
    connection, dialect = connect_item_store()
    
    if not connection:
        logger.error("Could not connect to the database. Ensure DATABASE_URL is set correctly if using MySQL.")
        return

    try:
        cursor = connection.cursor()
        
        logger.info("Backfilling 'listing_strategy' for existing items to 'auction'...")
        # Since the ALTER TABLE adds a DEFAULT 'auction', this is mostly a safety net for older items
        if dialect == "sqlite":
            cursor.execute("UPDATE auction_items SET listing_strategy = 'auction' WHERE listing_strategy IS NULL")
            cursor.execute("UPDATE auction_items SET platform_data = '{}' WHERE platform_data IS NULL")
        else:
            cursor.execute("UPDATE auction_items SET listing_strategy = 'auction' WHERE listing_strategy IS NULL")
            cursor.execute("UPDATE auction_items SET platform_data = '{}' WHERE platform_data IS NULL")
            
        connection.commit()
        logger.info("Migration completed successfully. The database is now ready for multi-platform retail support.")
        
    except Exception as e:
        logger.exception("An error occurred during migration: %s", e)
        connection.rollback()
    finally:
        connection.close()

if __name__ == "__main__":
    run_migration()
