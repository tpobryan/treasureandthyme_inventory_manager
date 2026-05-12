from datetime import datetime
from .extensions import db

class Auction(db.Model):
    __tablename__ = 'auctions'
    id = db.Column(db.Integer, primary_key=True)
    status = db.Column(db.String(50), nullable=False)
    is_current = db.Column(db.Integer, nullable=False, default=0)
    last_lot_override = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

class AuctionItem(db.Model):
    __tablename__ = 'auction_items'
    id = db.Column(db.Integer, primary_key=True)
    lot_number = db.Column(db.Integer, nullable=False)
    auction_id = db.Column(db.Integer, db.ForeignKey('auctions.id'), nullable=True)
    title = db.Column(db.Text, nullable=False)
    description = db.Column(db.Text, nullable=False)
    condition_notes = db.Column(db.Text, nullable=False)
    low_estimate = db.Column(db.String(50), nullable=False)
    high_estimate = db.Column(db.String(50), nullable=False)
    dimensions_length = db.Column(db.String(50), nullable=False)
    dimensions_depth = db.Column(db.String(50), nullable=False)
    dimensions_height = db.Column(db.String(50), nullable=False)
    tags = db.Column(db.Text, nullable=False)
    reference_number = db.Column(db.String(100), nullable=False)
    item_notes = db.Column(db.Text, nullable=False)
    consigner_number = db.Column(db.String(100), nullable=False)
    shipping_available = db.Column(db.String(50), nullable=False)
    category = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(50), nullable=False, default='ready')
    image_folder = db.Column(db.Text, nullable=False)
    last_export_batch = db.Column(db.String(255))
    published_at = db.Column(db.String(100))
    listing_strategy = db.Column(db.String(50), nullable=False, default='auction')
    platform_data = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=True, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=True, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('auction_id', 'lot_number', name='_auction_lot_uc'),)

class ItemPlatformStatus(db.Model):
    __tablename__ = 'item_platform_status'
    id = db.Column(db.Integer, primary_key=True)
    lot_number = db.Column(db.Integer, nullable=False)
    platform_id = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(50), nullable=False)
    remote_id = db.Column(db.String(255))
    published_at = db.Column(db.String(100))
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('lot_number', 'platform_id', name='_lot_platform_uc'),)

class Integration(db.Model):
    __tablename__ = 'integrations'
    id = db.Column(db.Integer, primary_key=True)
    platform_id = db.Column(db.String(50), nullable=False, unique=True)
    access_token = db.Column(db.Text)
    refresh_token = db.Column(db.Text)
    settings_json = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

class ExportBatch(db.Model):
    __tablename__ = 'export_batches'
    id = db.Column(db.Integer, primary_key=True)
    auction_id = db.Column(db.Integer, db.ForeignKey('auctions.id'), nullable=True)
    filename = db.Column(db.String(255), nullable=False, unique=True)
    export_type = db.Column(db.String(50), nullable=False)
    lot_numbers = db.Column(db.Text, nullable=False)
    lot_count = db.Column(db.Integer, nullable=False)
    archive_path = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

class FTPUpload(db.Model):
    __tablename__ = 'ftp_uploads'
    id = db.Column(db.Integer, primary_key=True)
    lot_number = db.Column(db.Integer, nullable=False)
    auction_id = db.Column(db.Integer, db.ForeignKey('auctions.id'), nullable=True)
    auction_number = db.Column(db.String(50), nullable=False)
    auction_photo_index = db.Column(db.Integer, nullable=False)
    remote_names = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('auction_id', 'lot_number', name='_ftp_auction_lot_uc'),)

class AuctionPhotoCounter(db.Model):
    __tablename__ = 'auction_photo_counters'
    auction_number = db.Column(db.String(50), primary_key=True)
    last_index = db.Column(db.Integer, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

class ActiveDraft(db.Model):
    __tablename__ = 'active_drafts'
    slot_name = db.Column(db.String(50), primary_key=True)
    temp_id = db.Column(db.String(100), nullable=False)
    owner_token = db.Column(db.String(100))
    seller_notes = db.Column(db.Text, nullable=False)
    options_json = db.Column(db.Text, nullable=False)
    form_json = db.Column(db.Text, nullable=True)
    revision_request = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=True, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=True, default=datetime.utcnow, onupdate=datetime.utcnow)
