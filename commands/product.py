"""/product command -- manage shop products."""
import asyncio
import re
import time
import uuid

import discord
from discord import app_commands
from discord.ext import commands

from utils.logger import log_command_activity
from utils.products import (
    add_group_whitelist,
    add_stock_batch,
    auto_revoke_product_for_user,
    auto_whitelist_product_for_user,
    bump_product_version,
    create_or_sync_product_type_forum,
    delete_product,
    get_product,
    get_products_by_ids,
    give_product_to_user,
    is_in_stock,
    link_existing_forum_to_type,
    list_product_types,
    list_products_by_guild,
    list_products_by_type,
    product_version,
    remove_group_whitelist,
    revoke_product_from_user,
    save_product,
    stock_summary_text,
    user_owns_product,
    build_product_delivery_dm,
    build_rating_embed,
)
from utils.reviews import RATING_MAX, RATING_MIN, get_testimony_count, set_testimony_count, submit_review
from utils.reviews_channel import get_reviews_channel, save_reviews_channel, save_reviews_mod_role
from utils.verification import get_verified_user
from utils.tickets import get_ticket
from utils.payments import (
    PaymentGatewayError,
    confirm_payment,
    create_payment_order,
    find_pending_order_for_channel,
    mark_order_status,
    pakasir_configured,
)

COMMAND_NAME = 'product'

LOG_SCHEMA = {
    'subcommands': {
        'create': {'label': 'Product — Created', 'fields': ['discordUser', 'productId', 'productName']},
        'createtype': {'label': 'Product — Type Created', 'fields': ['discordUser', 'typeName', 'forumChannel']},
        'linktype': {'label': 'Product — Type Linked', 'fields': ['discordUser', 'typeName', 'forumChannel']},
        'sendpost': {'label': 'Product — Post Sent', 'fields': ['discordUser', 'productId', 'forumChannel']},
        'edit': {'label': 'Product — Edited', 'fields': ['discordUser', 'productId', 'productName']},
        'update': {'label': 'Product — Version Updated', 'fields': ['discordUser', 'productId', 'productName', 'version']},
        'setstock': {'label': 'Product — Stock Added', 'fields': ['discordUser', 'productId', 'productName', 'quantity']},
        'buy': {'label': 'Product — QRIS Payment Created', 'fields': ['discordUser', 'productId', 'productName']},
        'view': {'label': 'Product — Browsed', 'fields': ['discordUser']},
        'delete': {'label': 'Product — Deleted', 'fields': ['discordUser', 'productId', 'productName']},
        'give': {'label': 'Product — Given', 'fields': ['discordUser', 'targetUser', 'productId', 'productName']},
        'revoke': {'label': 'Product — Revoked', 'fields': ['discordUser', 'targetUser', 'productId', 'productName']},
        'get': {'label': 'Product — File Link Requested', 'fields': ['discordUser', 'productId', 'productName']},
        'groupwhitelistadd': {'label': 'Product — Group Whitelisted', 'fields': ['discordUser', 'productId', 'groupId']},
        'groupwhitelistremove': {'label': 'Product — Group Whitelist Removed', 'fields': ['discordUser', 'productId', 'groupId']},
        'rating': {'label': 'Product — Rated', 'fields': ['discordUser', 'productId', 'productName', 'rating']},
        'settesticount': {'label': 'Product — Guild Testimony Count Edited', 'fields': ['discordUser', 'count']},
        'setreviewschannel': {'label': 'Product — Reviews Channel Set', 'fields': ['discordUser', 'reviewsChannel', 'modRole']},
    },
}

STEP_TIMEOUT_S = 15 * 60
MAX_SELECT_OPTIONS = 25
_IMAGE_URL_RE = re.compile(r'\.(png|jpe?g|gif|webp)(\?.*)?$', re.IGNORECASE)
_URL_RE = re.compile(r'^https?://', re.IGNORECASE)
_DIGITS_RE = re.compile(r'[^0-9]')
QRIS_POLL_INTERVAL_S = 5
QRIS_POLL_TIMEOUT_S = 30 * 60


def _now_ms():
    return int(time.time() * 1000)


def _require_admin(interaction: discord.Interaction) -> bool:
    return bool(interaction.user.guild_permissions.administrator)


def _format_idr_local(n) -> str:
    return f'Rp{int(n):,}'.replace(',', '.')


def _parse_price_local(price_str) -> int:
    digits = _DIGITS_RE.sub('', str(price_str or ''))
    return int(digits) if digits else 0


def _render_qris_image(qris_string: str, order_id: str) -> discord.File:
    """Renders the QRIS payload as a PNG locally (never via a third-party
    image URL -- the QRIS string carries live payment routing data)."""
    import io
    import qrcode

    img = qrcode.make(qris_string or '')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', order_id)[:60]
    return discord.File(buf, filename=f'qris_{safe_name}.png')


def _is_free_product(price) -> bool:
    normalized = str(price or '').strip().lower()
    return normalized in ('0', 'free')


async def _admin_denied(interaction: discord.Interaction):
    await interaction.response.send_message('You need **Administrator** permission to do that.', ephemeral=True)


class CreateModal1(discord.ui.Modal, title='New Product (1/2)'):
    product_name = discord.ui.TextInput(label='Nama produk', style=discord.TextStyle.short, required=True)
    product_description = discord.ui.TextInput(label='Deskripsi', style=discord.TextStyle.paragraph, required=True)
    product_price = discord.ui.TextInput(label='Harga', placeholder='cth: 25000 atau Rp25.000', style=discord.TextStyle.short, required=True)
    product_creator = discord.ui.TextInput(label='Kreator (kosongkan jika kamu sendiri)', style=discord.TextStyle.short, required=False)

    def __init__(self, source_interaction: discord.Interaction):
        super().__init__()
        self.source_interaction = source_interaction

    async def on_submit(self, interaction: discord.Interaction):
        name = self.product_name.value.strip()
        description = self.product_description.value.strip()
        price = self.product_price.value.strip()
        creator = self.product_creator.value.strip() or interaction.user.name

        view = discord.ui.View(timeout=STEP_TIMEOUT_S)
        continue_btn = discord.ui.Button(label='Lanjutkan (2/2)', style=discord.ButtonStyle.primary)

        async def _continue_cb(btn_interaction: discord.Interaction):
            modal2 = CreateModal2(
                source_interaction=self.source_interaction,
                name=name, description=description, price=price, creator=creator,
            )
            await btn_interaction.response.send_modal(modal2)

        continue_btn.callback = _continue_cb
        view.add_item(continue_btn)

        async def _on_timeout():
            try:
                await interaction.edit_original_response(content='Waktu habis. Jalankan `/product create` lagi.', view=None)
            except discord.HTTPException:
                pass

        view.on_timeout = _on_timeout

        await interaction.response.send_message(
            'Langkah 1 tersimpan. Klik tombol di bawah buat lanjut ke langkah 2.', view=view, ephemeral=True
        )


class CreateModal2(discord.ui.Modal, title='New Product (2/2)'):
    product_file_link = discord.ui.TextInput(label='Link file produk', placeholder='CDN Discord, catbox.moe, Drive, Mega.nz, dll', style=discord.TextStyle.short, required=True)
    product_review_media = discord.ui.TextInput(label='Video/Gambar Review Produk', placeholder='Link video atau gambar review', style=discord.TextStyle.short, required=True)
    product_tutorial_link = discord.ui.TextInput(label='Link Tutorial (opsional)', placeholder='Link tutorial cara pakai produk, boleh kosong', style=discord.TextStyle.short, required=False)

    def __init__(self, *, source_interaction: discord.Interaction, name, description, price, creator):
        super().__init__()
        self.source_interaction = source_interaction
        self.name = name
        self.description = description
        self.price = price
        self.creator = creator

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        file_link = self.product_file_link.value.strip()
        review_media = self.product_review_media.value.strip()
        tutorial_link = self.product_tutorial_link.value.strip()

        types = await list_product_types(str(interaction.guild_id))
        if not types:
            return await interaction.followup.send(
                'Belum ada jenis produk yang terdaftar. Minta admin jalankan `/product createtype` dulu, baru ulangi `/product create`.'
            )

        select = discord.ui.Select(
            placeholder='Pilih jenis produk',
            options=[discord.SelectOption(label=t['name'], value=t['id']) for t in types[:MAX_SELECT_OPTIONS]],
        )
        view = discord.ui.View(timeout=STEP_TIMEOUT_S)
        view.add_item(select)

        async def _on_timeout():
            try:
                await interaction.edit_original_response(content='Waktu habis memilih jenis. Jalankan `/product create` lagi.', view=None)
            except discord.HTTPException:
                pass

        view.on_timeout = _on_timeout

        async def _select_cb(select_interaction: discord.Interaction):
            selected_type_id = select.values[0]
            selected_type = next((t for t in types if t['id'] == selected_type_id), None)

            try:
                await select_interaction.response.defer_update() if hasattr(select_interaction.response, 'defer_update') else await select_interaction.response.defer()
            except Exception as err:
                print(f'select deferred failed (continuing anyway): {err}')

            product_id = str(uuid.uuid4())
            product_data = {
                'productId': product_id,
                'name': self.name,
                'description': self.description,
                'price': self.price,
                'fileLink': file_link,
                'tutorialLink': tutorial_link,
                'reviewMedia': review_media,
                'creator': self.creator,
                'type': selected_type['name'],
                'typeId': selected_type['id'],
                'typeForumId': selected_type.get('forumChannelId'),
                'createdBy': str(interaction.user.id),
                'guildId': str(interaction.guild_id),
                'createdAt': _now_ms(),
            }

            try:
                await save_product(product_id, product_data)
            except Exception as err:
                print(f'Failed to save product to Firestore: {err}')
                await log_command_activity(
                    self.source_interaction, subcommand='create', success=False,
                    fields={'discordUser': interaction.user, 'productName': self.name}, note='Firestore write failed.',
                )
                return await interaction.edit_original_response(content='Gagal menyimpan produk ke database. Coba lagi.', view=None)

            await log_command_activity(
                self.source_interaction, subcommand='create', success=True,
                fields={'discordUser': interaction.user, 'productId': product_id, 'productName': self.name},
            )

            embed = discord.Embed(title='Produk Berhasil Dibuat', color=0x57F287)
            embed.add_field(name='Nama Produk', value=self.name, inline=False)
            embed.add_field(name='ID Produk', value=f'`{product_id}`', inline=False)
            embed.add_field(name='Jenis', value=selected_type['name'], inline=True)
            embed.add_field(name='Harga', value=self.price, inline=True)
            embed.add_field(name='Kreator', value=self.creator, inline=True)

            await interaction.edit_original_response(content='Produk berhasil dibuat!', embed=embed, view=None)

        select.callback = _select_cb

        await interaction.followup.send('Terakhir, pilih jenis produk:', view=view)


class EditModal1(discord.ui.Modal, title='Edit Product (1/2)'):
    def __init__(self, source_interaction: discord.Interaction, product: dict, product_id: str):
        super().__init__()
        self.source_interaction = source_interaction
        self.product = product
        self.product_id = product_id

        self.product_name = discord.ui.TextInput(label='Nama produk', style=discord.TextStyle.short, default=product['name'], required=True)
        self.product_description = discord.ui.TextInput(label='Deskripsi', style=discord.TextStyle.paragraph, default=product['description'], required=True)
        self.product_price = discord.ui.TextInput(label='Harga', placeholder='cth: 25000 atau Rp25.000', style=discord.TextStyle.short, default=product['price'], required=True)
        self.product_creator = discord.ui.TextInput(label='Kreator (kosongkan jika kamu sendiri)', style=discord.TextStyle.short, default=product.get('creator', ''), required=False)

        for item in (self.product_name, self.product_description, self.product_price, self.product_creator):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.product_name.value.strip()
        description = self.product_description.value.strip()
        price = self.product_price.value.strip()
        creator = self.product_creator.value.strip() or interaction.user.name

        view = discord.ui.View(timeout=STEP_TIMEOUT_S)
        continue_btn = discord.ui.Button(label='Lanjutkan (2/2)', style=discord.ButtonStyle.primary)

        async def _continue_cb(btn_interaction: discord.Interaction):
            modal2 = EditModal2(
                source_interaction=self.source_interaction, product=self.product, product_id=self.product_id,
                name=name, description=description, price=price, creator=creator,
            )
            await btn_interaction.response.send_modal(modal2)

        continue_btn.callback = _continue_cb
        view.add_item(continue_btn)

        async def _on_timeout():
            try:
                await interaction.edit_original_response(content=f'Waktu habis. Jalankan `/product edit {self.product_id}` lagi.', view=None)
            except discord.HTTPException:
                pass

        view.on_timeout = _on_timeout

        await interaction.response.send_message(
            'Langkah 1 tersimpan. Klik tombol di bawah buat lanjut ke langkah 2.', view=view, ephemeral=True
        )


class EditModal2(discord.ui.Modal, title='Edit Product (2/2)'):
    def __init__(self, *, source_interaction: discord.Interaction, product: dict, product_id: str, name, description, price, creator):
        super().__init__()
        self.source_interaction = source_interaction
        self.product = product
        self.product_id = product_id
        self.name = name
        self.description = description
        self.price = price
        self.creator = creator

        self.product_file_link = discord.ui.TextInput(label='Link file produk', placeholder='CDN Discord, catbox.moe, Drive, Mega.nz, dll', style=discord.TextStyle.short, default=product['fileLink'], required=True)
        self.product_review_media = discord.ui.TextInput(label='Video/Gambar Review Produk', placeholder='Link video atau gambar review', style=discord.TextStyle.short, default=product.get('reviewMedia', ''), required=True)
        self.product_tutorial_link = discord.ui.TextInput(label='Link Tutorial (opsional)', placeholder='Link tutorial cara pakai produk, boleh kosong', style=discord.TextStyle.short, default=product.get('tutorialLink', ''), required=False)

        for item in (self.product_file_link, self.product_review_media, self.product_tutorial_link):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        file_link = self.product_file_link.value.strip()
        review_media = self.product_review_media.value.strip()
        tutorial_link = self.product_tutorial_link.value.strip()

        types = await list_product_types(str(interaction.guild_id))
        if not types:
            return await interaction.followup.send('Belum ada jenis produk yang terdaftar. Minta admin jalankan `/product createtype` dulu.')

        select = discord.ui.Select(
            placeholder='Pilih jenis produk',
            options=[
                discord.SelectOption(label=t['name'], value=t['id'], default=(t['id'] == self.product.get('typeId')))
                for t in types[:MAX_SELECT_OPTIONS]
            ],
        )
        view = discord.ui.View(timeout=STEP_TIMEOUT_S)
        view.add_item(select)

        keep_btn = discord.ui.Button(label=f"Simpan dengan jenis \"{self.product['type']}\"", style=discord.ButtonStyle.secondary)
        view.add_item(keep_btn)

        async def _on_timeout():
            try:
                await interaction.edit_original_response(content=f'Waktu habis memilih jenis. Jalankan `/product edit {self.product_id}` lagi.', view=None)
            except discord.HTTPException:
                pass

        view.on_timeout = _on_timeout

        async def _finish(select_interaction: discord.Interaction, selected_type_id: str):
            selected_type = next((t for t in types if t['id'] == selected_type_id), None)

            try:
                await select_interaction.response.defer()
            except Exception as err:
                print(f'ack deferred failed (continuing anyway): {err}')

            updated_data = {
                **self.product,
                'productId': self.product_id,
                'name': self.name,
                'description': self.description,
                'price': self.price,
                'fileLink': file_link,
                'tutorialLink': tutorial_link,
                'reviewMedia': review_media,
                'creator': self.creator,
                'type': selected_type['name'],
                'typeId': selected_type['id'],
                'typeForumId': (
                    self.product.get('typeForumId')
                    if selected_type['id'] == self.product.get('typeId')
                    else selected_type.get('forumChannelId')
                ),
                'updatedAt': _now_ms(),
            }
            updated_data.pop('id', None)

            try:
                await save_product(self.product_id, updated_data)
            except Exception as err:
                print(f'Failed to save edited product to Firestore: {err}')
                await log_command_activity(
                    self.source_interaction, subcommand='edit', success=False,
                    fields={'discordUser': interaction.user, 'productId': self.product_id, 'productName': self.name},
                    note='Firestore write failed.',
                )
                return await interaction.edit_original_response(content='Gagal menyimpan perubahan produk ke database. Coba lagi.', view=None)

            await log_command_activity(
                self.source_interaction, subcommand='edit', success=True,
                fields={'discordUser': interaction.user, 'productId': self.product_id, 'productName': self.name},
            )

            embed = discord.Embed(title='Produk Berhasil Diedit', color=0x57F287)
            embed.add_field(name='Nama Produk', value=self.name, inline=False)
            embed.add_field(name='ID Produk', value=f'`{self.product_id}`', inline=False)
            embed.add_field(name='Jenis', value=selected_type['name'], inline=True)
            embed.add_field(name='Harga', value=self.price, inline=True)
            embed.add_field(name='Kreator', value=self.creator, inline=True)

            post_note = ' Jalankan `/product sendpost` untuk update post forum-nya juga.' if updated_data.get('forumThreadId') else ''

            await interaction.edit_original_response(content=f'Produk berhasil diedit!{post_note}', embed=embed, view=None)

        async def _select_cb(select_interaction: discord.Interaction):
            await _finish(select_interaction, select.values[0])

        async def _keep_cb(select_interaction: discord.Interaction):
            await _finish(select_interaction, self.product.get('typeId'))

        select.callback = _select_cb
        keep_btn.callback = _keep_cb

        await interaction.followup.send(
            'Terakhir, pilih jenis produk (atau klik tombol untuk tetap pakai jenis saat ini):', view=view
        )


class UpdateModal(discord.ui.Modal, title='Update Product Version'):
    changelog = discord.ui.TextInput(
        label='Apa yang berubah?', style=discord.TextStyle.paragraph,
        placeholder='cth: Perbaikan bug, fitur baru ditambahkan, dll', required=True,
    )

    def __init__(self, source_interaction: discord.Interaction, product: dict, product_id: str):
        super().__init__()
        self.source_interaction = source_interaction
        self.product = product
        self.product_id = product_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        changelog_text = self.changelog.value.strip()

        try:
            new_version = await bump_product_version(self.product_id, changelog_text, str(interaction.user.id))
        except Exception as err:
            print(f'bump_product_version failed: {err}')
            await log_command_activity(
                self.source_interaction, subcommand='update', success=False,
                fields={'discordUser': interaction.user, 'productId': self.product_id, 'productName': self.product.get('name')},
                note='Firestore write failed.',
            )
            return await interaction.followup.send('Gagal menaikkan versi produk. Coba lagi.')

        await log_command_activity(
            self.source_interaction, subcommand='update', success=True,
            fields={
                'discordUser': interaction.user, 'productId': self.product_id,
                'productName': self.product.get('name'), 'version': new_version,
            },
        )

        embed = discord.Embed(title=f"{self.product['name']} diperbarui ke v{new_version}", color=0x57F287)
        embed.add_field(name='Perubahan', value=changelog_text, inline=False)
        embed.set_footer(text='Stok & link file TIDAK berubah -- pakai /product setstock kalau perlu ganti/tambah link.')

        await interaction.followup.send(embed=embed)


class SetStockModal(discord.ui.Modal, title='Set / Add Stock'):
    stock_link = discord.ui.TextInput(
        label='Link file (untuk batch stok ini)', style=discord.TextStyle.short,
        placeholder='CDN Discord, catbox.moe, Drive, Mega.nz, dll', required=True,
    )
    stock_quantity = discord.ui.TextInput(
        label='Jumlah stok', style=discord.TextStyle.short,
        placeholder='cth: 20, atau ketik "infinite" / "unlimited"', required=True,
    )

    def __init__(self, source_interaction: discord.Interaction, product: dict, product_id: str):
        super().__init__()
        self.source_interaction = source_interaction
        self.product = product
        self.product_id = product_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        file_link = self.stock_link.value.strip()
        quantity_raw = self.stock_quantity.value.strip().lower()

        if quantity_raw in ('infinite', 'unlimited', 'inf', 'tak terbatas', 'unlimited stock', '-1'):
            quantity = None
        else:
            digits = re.sub(r'[^0-9]', '', quantity_raw)
            if not digits or int(digits) <= 0:
                return await interaction.followup.send(
                    'Jumlah stok harus angka positif, atau ketik "infinite" untuk stok tak terbatas.'
                )
            quantity = int(digits)

        try:
            pool = await add_stock_batch(self.product_id, file_link, quantity)
        except Exception as err:
            print(f'add_stock_batch failed: {err}')
            await log_command_activity(
                self.source_interaction, subcommand='setstock', success=False,
                fields={'discordUser': interaction.user, 'productId': self.product_id, 'productName': self.product.get('name')},
                note='Firestore write failed.',
            )
            return await interaction.followup.send('Gagal menyimpan stok. Coba lagi.')

        await log_command_activity(
            self.source_interaction, subcommand='setstock', success=True,
            fields={
                'discordUser': interaction.user, 'productId': self.product_id,
                'productName': self.product.get('name'),
                'quantity': 'infinite' if quantity is None else quantity,
            },
        )

        qty_text = 'Tidak terbatas' if quantity is None else str(quantity)
        embed = discord.Embed(title=f"Stok ditambahkan: {self.product['name']}", color=0x57F287)
        embed.add_field(name='Batch baru', value=f'{qty_text} unit', inline=True)
        embed.add_field(name='Total batch aktif', value=str(len(pool)), inline=True)
        embed.set_footer(text='Setiap batch bisa punya link file berbeda -- pembeli akan mendapat salah satu link secara acak.')

        await interaction.followup.send(embed=embed)


class DeleteConfirmModal(discord.ui.Modal, title='Confirm Delete'):
    def __init__(self, source_interaction: discord.Interaction, product_id: str):
        super().__init__()
        self.source_interaction = source_interaction
        self.product_id = product_id
        self.confirm_uuid = discord.ui.TextInput(
            label='Ketik ulang UUID produk untuk konfirmasi', placeholder=product_id, style=discord.TextStyle.short, required=True
        )
        self.add_item(self.confirm_uuid)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        product = await get_product(self.product_id)
        if not product:
            await log_command_activity(
                self.source_interaction, subcommand='delete', success=False,
                fields={'discordUser': interaction.user, 'productId': self.product_id}, note='Product UUID not found.',
            )
            return await interaction.followup.send(f'Produk dengan ID `{self.product_id}` tidak ditemukan.')

        typed = self.confirm_uuid.value.strip()
        if typed != self.product_id:
            return await interaction.followup.send(
                f'UUID tidak cocok. Kamu ketik `{typed}`, seharusnya `{self.product_id}`. Jalankan `/product delete` lagi untuk mengulang.'
            )

        if product.get('forumThreadId') and product.get('typeForumId'):
            try:
                guild = interaction.guild
                forum_channel = guild.get_channel(int(product['typeForumId']))
                if forum_channel is None:
                    forum_channel = await guild.fetch_channel(int(product['typeForumId']))
                if forum_channel:
                    thread = guild.get_thread(int(product['forumThreadId']))
                    if thread is None:
                        thread = await guild.fetch_channel(int(product['forumThreadId']))
                    if thread:
                        await thread.delete()
            except Exception as err:
                print(f'Failed to delete forum thread during product delete (continuing anyway): {err}')

        try:
            await delete_product(self.product_id)
        except Exception as err:
            print(f'Failed to delete product from Firestore: {err}')
            await log_command_activity(
                self.source_interaction, subcommand='delete', success=False,
                fields={'discordUser': interaction.user, 'productId': self.product_id, 'productName': product['name']},
                note='Firestore delete failed.',
            )
            return await interaction.followup.send('Gagal menghapus produk dari database. Coba lagi.')

        await log_command_activity(
            self.source_interaction, subcommand='delete', success=True,
            fields={'discordUser': interaction.user, 'productId': self.product_id, 'productName': product['name']},
        )

        return await interaction.followup.send(f"Produk **{product['name']}** (`{self.product_id}`) berhasil dihapus.")


class RatingModal(discord.ui.Modal, title='Rate this product'):
    rating_input = discord.ui.TextInput(
        label=f'Rating ({RATING_MIN}-{RATING_MAX})', placeholder='e.g. 9', style=discord.TextStyle.short,
        max_length=2, required=True,
    )
    reason_input = discord.ui.TextInput(
        label='Reason', placeholder='Kenapa kasih rating segitu?', style=discord.TextStyle.paragraph, required=True,
    )

    def __init__(self, source_interaction: discord.Interaction, product: dict, product_id: str, *, ticket_channel_id: str | None = None):
        super().__init__()
        self.source_interaction = source_interaction
        self.product = product
        self.product_id = product_id
        self.ticket_channel_id = ticket_channel_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        raw = self.rating_input.value.strip()
        if not raw.isdigit() or not (RATING_MIN <= int(raw) <= RATING_MAX):
            return await interaction.followup.send(
                f'Rating harus angka {RATING_MIN}-{RATING_MAX}. Jalankan `/product rating` lagi.',
                ephemeral=True,
            )

        rating = int(raw)
        reason = self.reason_input.value.strip()

        try:
            result = await submit_review(
                self.product_id, str(interaction.user.id), interaction.user.name, rating, reason,
                ticket_channel_id=self.ticket_channel_id,
            )
        except Exception as err:
            print(f'Failed to save review to Firestore: {err}')
            await log_command_activity(
                self.source_interaction, subcommand='rating', success=False,
                fields={'discordUser': interaction.user, 'productId': self.product_id, 'productName': self.product['name'], 'rating': rating},
                note='Firestore write failed.',
            )
            return await interaction.followup.send('Gagal menyimpan rating ke database. Coba lagi.', ephemeral=True)

        rating_embed = build_rating_embed(self.product, rating, reason, interaction.user.name)

        post_note = ''
        posted_publicly = False
        guild = interaction.guild or self.source_interaction.guild

        if guild is not None:
            try:
                reviews_channel = await get_reviews_channel(guild, str(guild.id))
                if reviews_channel is not None:
                    await reviews_channel.send(embed=rating_embed)
                    posted_publicly = True
            except Exception as err:
                print(f'Failed to post rating to configured reviews channel: {err}')

        if not posted_publicly:
            forum_id = self.product.get('typeForumId')
            thread_id = self.product.get('forumThreadId')
            if forum_id and thread_id and guild is not None:
                try:
                    thread = guild.get_thread(int(thread_id))
                    if thread is None:
                        thread = await guild.fetch_channel(int(thread_id))
                    if thread:
                        await thread.send(embed=rating_embed)
                        posted_publicly = True
                except Exception as err:
                    print(f'Failed to post rating to forum thread: {err}')

        if not posted_publicly:
            try:
                target_channel = interaction.channel
                if target_channel is None and guild is not None:
                    target_channel = guild.get_channel(interaction.channel_id) or await guild.fetch_channel(interaction.channel_id)
                if target_channel is not None:
                    await target_channel.send(embed=rating_embed)
                    posted_publicly = True
                else:
                    print('Failed to post rating embed: could not resolve a channel to post in.')
                    post_note = ' (Gagal posting embed rating, tapi rating sudah tersimpan.)'
            except Exception as err:
                print(f'Failed to post rating embed to channel: {err}')
                post_note = ' (Gagal posting embed rating, tapi rating sudah tersimpan.)'

        await log_command_activity(
            self.source_interaction, subcommand='rating', success=True,
            fields={'discordUser': interaction.user, 'productId': self.product_id, 'productName': self.product['name'], 'rating': rating},
        )

        verb = 'diperbarui' if result['wasUpdate'] else 'tersimpan'
        await interaction.followup.send(
            f"Rating kamu **{rating}/10** untuk **{self.product['name']}** {verb}. "
            f"Rata-rata sekarang **{result['reviewAvg']}/10** dari {result['reviewCount']} rating.{post_note}",
            ephemeral=True,
        )


class ProductViewView(discord.ui.View):
    def __init__(self, *, owner_id: int, guild_id: str, types: list[dict]):
        super().__init__(timeout=10 * 60)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.types = types
        self.type_index = 0
        self.product_index = 0
        self._products_cache: dict[str, list[dict]] = {}
        self.message: discord.Message | None = None
        self._build_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message('Only the person who ran this command can use these buttons.', ephemeral=True)
            return False
        return True

    async def _get_products(self, type_index: int) -> list[dict]:
        t = self.types[type_index]
        if t['id'] not in self._products_cache:
            products = await list_products_by_type(self.guild_id, t['id'])
            products.sort(key=lambda p: p['name'])
            self._products_cache[t['id']] = products
        return self._products_cache[t['id']]

    async def build_embed(self) -> discord.Embed:
        t = self.types[self.type_index]
        products = await self._get_products(self.type_index)

        embed = discord.Embed(color=0x00B0F4)
        embed.set_footer(text=f'Jenis {self.type_index + 1}/{len(self.types)} — {t["name"]}')

        if not products:
            embed.title = t['name']
            embed.description = 'Belum ada produk di jenis ini.'
            return embed

        product = products[self.product_index]
        embed.title = product['name']
        embed.description = product['description']
        embed.add_field(name='Harga', value=product['price'], inline=True)
        embed.add_field(name='Jenis', value=product['type'], inline=True)
        embed.add_field(name='Kreator', value=product['creator'], inline=True)
        embed.add_field(name='Versi', value=f"v{product_version(product)}", inline=True)
        embed.add_field(name='Stok', value=stock_summary_text(product), inline=True)
        embed.add_field(name='ID Produk', value=f'`{product["productId"]}`', inline=False)

        if product.get('reviewCount'):
            embed.add_field(name='Rating', value=f"⭐ {product['reviewAvg']}/10 ({product['reviewCount']} rating)", inline=True)
        testi_count = await get_testimony_count(self.guild_id)
        if testi_count:
            embed.add_field(name='Testimoni', value=str(testi_count), inline=True)

        review_media = product.get('reviewMedia') or ''
        if _IMAGE_URL_RE.search(review_media):
            embed.set_image(url=review_media)
        elif review_media:
            embed.add_field(name='Video/Gambar Review', value=review_media, inline=False)

        embed.set_footer(text=f'Jenis {self.type_index + 1}/{len(self.types)} — {t["name"]} · Produk {self.product_index + 1}/{len(products)}')
        return embed

    def _build_components(self, disabled: bool = False):
        self.clear_items()
        products = self._products_cache.get(self.types[self.type_index]['id'], [])

        type_prev = discord.ui.Button(label='◀◀ Jenis', style=discord.ButtonStyle.secondary, disabled=disabled or len(self.types) <= 1)
        product_prev = discord.ui.Button(label='◀ Produk', style=discord.ButtonStyle.secondary, disabled=disabled or len(products) <= 1 or self.product_index == 0)
        product_next = discord.ui.Button(label='Produk ▶', style=discord.ButtonStyle.secondary, disabled=disabled or len(products) <= 1 or self.product_index >= max(len(products) - 1, 0))
        type_next = discord.ui.Button(label='Jenis ▶▶', style=discord.ButtonStyle.secondary, disabled=disabled or len(self.types) <= 1)

        async def _type_prev_cb(interaction: discord.Interaction):
            self.type_index = (self.type_index - 1) % len(self.types)
            self.product_index = 0
            await self._get_products(self.type_index)
            self._build_components()
            await interaction.response.edit_message(embed=await self.build_embed(), view=self)

        async def _type_next_cb(interaction: discord.Interaction):
            self.type_index = (self.type_index + 1) % len(self.types)
            self.product_index = 0
            await self._get_products(self.type_index)
            self._build_components()
            await interaction.response.edit_message(embed=await self.build_embed(), view=self)

        async def _product_prev_cb(interaction: discord.Interaction):
            self.product_index = max(0, self.product_index - 1)
            self._build_components()
            await interaction.response.edit_message(embed=await self.build_embed(), view=self)

        async def _product_next_cb(interaction: discord.Interaction):
            products = await self._get_products(self.type_index)
            self.product_index = min(len(products) - 1, self.product_index + 1)
            self._build_components()
            await interaction.response.edit_message(embed=await self.build_embed(), view=self)

        type_prev.callback = _type_prev_cb
        product_prev.callback = _product_prev_cb
        product_next.callback = _product_next_cb
        type_next.callback = _type_next_cb

        for btn in (type_prev, product_prev, product_next, type_next):
            self.add_item(btn)

    async def on_timeout(self):
        self._build_components(disabled=True)
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def _autocomplete_get_product_uuid(interaction: discord.Interaction, current: str):
    focused = (current or '').lower()

    verified_record = await get_verified_user(str(interaction.user.id))
    owned_ids = (verified_record or {}).get('ownedProducts')
    if not owned_ids:
        return []

    owned = await get_products_by_ids(owned_ids)
    filtered = [p for p in owned if focused in (p.get('name') or '').lower()][:25]
    return [app_commands.Choice(name=p['name'][:100], value=p['id']) for p in filtered]


async def _autocomplete_any_product_uuid(interaction: discord.Interaction, current: str):
    if interaction.guild_id is None:
        return []

    focused = (current or '').lower()
    products = await list_products_by_guild(str(interaction.guild_id))
    filtered = [p for p in products if focused in (p.get('name') or '').lower()][:25]
    return [
        app_commands.Choice(name=f"{p['name']} ({p['id'][:8]})"[:100], value=p['id'])
        for p in filtered
    ]


_LIVE_QRIS_VIEWS: dict[str, 'QRISPaymentView'] = {}


async def notify_order_paid(bot: commands.Bot, order_id: str, order: dict):
    """Called by utils/webhook_server when Pakasir's webhook confirms a
    payment. Delivers immediately if we still have the live view for this
    order in memory; otherwise this is a no-op and the view's own polling
    loop (or a fresh /product buy poll after a restart) will pick it up.
    """
    view = _LIVE_QRIS_VIEWS.get(order_id)
    if view is not None:
        await view._deliver(bot, order)


class QRISPaymentView(discord.ui.View):
    def __init__(self, *, order_id: str, product: dict, buyer_id: str):
        super().__init__(timeout=QRIS_POLL_TIMEOUT_S)
        self.order_id = order_id
        self.product = product
        self.buyer_id = buyer_id
        self.message: discord.Message | None = None
        self._delivered = False
        self._poll_task: asyncio.Task | None = None
        _LIVE_QRIS_VIEWS[order_id] = self

    def start_polling(self, client: discord.Client):
        self._poll_task = asyncio.create_task(self._poll_loop(client))

    async def _poll_loop(self, client: discord.Client):
        elapsed = 0
        while elapsed < QRIS_POLL_TIMEOUT_S and not self._delivered:
            await asyncio.sleep(QRIS_POLL_INTERVAL_S)
            elapsed += QRIS_POLL_INTERVAL_S
            try:
                order = await confirm_payment(self.order_id)
            except PaymentGatewayError as err:
                print(f'[product buy] poll confirm_payment failed for {self.order_id}: {err}')
                continue
            if order:
                await self._deliver(client, order)
                return

        if not self._delivered:
            _LIVE_QRIS_VIEWS.pop(self.order_id, None)
            await mark_order_status(self.order_id, 'expired')
            if self.message:
                try:
                    embed = self.message.embeds[0]
                    embed.set_field_at(1, name='Status', value='⌛ Kadaluarsa', inline=True)
                    for item in self.children:
                        item.disabled = True
                    await self.message.edit(embed=embed, view=self)
                except discord.HTTPException:
                    pass

    async def _deliver(self, client: discord.Client, order: dict):
        if self._delivered:
            return
        self._delivered = True
        _LIVE_QRIS_VIEWS.pop(self.order_id, None)
        for item in self.children:
            item.disabled = True

        file_link = await draw_stock_unit(self.product['id'])

        try:
            await give_product_to_user(self.product['id'], self.buyer_id)
            await auto_whitelist_product_for_user(self.product['id'], self.buyer_id)
        except Exception as err:
            print(f"Failed to grant product {self.product['id']} to {self.buyer_id} after payment: {err}")

        buyer = client.get_user(int(self.buyer_id))
        if buyer is None:
            try:
                buyer = await client.fetch_user(int(self.buyer_id))
            except discord.HTTPException:
                buyer = None

        if buyer and file_link:
            try:
                await buyer.send(**build_product_delivery_dm(self.product, file_link=file_link))
            except discord.HTTPException:
                pass

        if self.message:
            try:
                embed = self.message.embeds[0]
                embed.set_field_at(1, name='Status', value='✅ Lunas -- produk dikirim ke DM', inline=True)
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label='Cek Pembayaran', style=discord.ButtonStyle.primary, emoji='🔄')
    async def check_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.buyer_id:
            return await interaction.response.send_message('Hanya pembeli yang bisa mengecek pembayaran ini.', ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        try:
            order = await confirm_payment(self.order_id)
        except PaymentGatewayError as err:
            print(f'[product buy] manual confirm_payment failed for {self.order_id}: {err}')
            return await interaction.followup.send('Gagal mengecek status pembayaran. Coba lagi sebentar lagi.', ephemeral=True)

        if not order:
            return await interaction.followup.send('Belum ada pembayaran yang terdeteksi. Coba lagi setelah selesai scan.', ephemeral=True)

        await self._deliver(interaction.client, order)
        await interaction.followup.send('✅ Pembayaran terkonfirmasi, produk sudah dikirim ke DM kamu!', ephemeral=True)

    async def on_timeout(self):
        if self._delivered:
            return
        _LIVE_QRIS_VIEWS.pop(self.order_id, None)
        await mark_order_status(self.order_id, 'expired')
        if self.message:
            try:
                for item in self.children:
                    item.disabled = True
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ProductCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    product_group = app_commands.Group(name='product', description='Manage shop products')

    async def _guild_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message('This command only works inside a server.', ephemeral=True)
            return False
        return True

    @product_group.command(name='create', description='Create a new product listing')
    async def create(self, interaction: discord.Interaction):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)
        await interaction.response.send_modal(CreateModal1(source_interaction=interaction))

    @product_group.command(name='createtype', description='Create a product type, optionally linking an existing forum instead of making one')
    @app_commands.describe(nama='Nama jenis produk', channel='Opsional: forum channel yang sudah ada untuk dihubungkan (kosongkan untuk membuat forum baru otomatis)')
    async def createtype(self, interaction: discord.Interaction, nama: str, channel: discord.ForumChannel | None = None):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        await interaction.response.defer(ephemeral=True)

        type_name = nama.strip()
        if not type_name:
            return await interaction.followup.send('Nama jenis tidak boleh kosong.')

        if channel is not None:
            try:
                result = await link_existing_forum_to_type(interaction.guild, str(interaction.guild_id), type_name, channel)
            except Exception as err:
                print(f'link_existing_forum_to_type failed (via createtype): {err}')
                await log_command_activity(
                    interaction, subcommand='createtype', success=False,
                    fields={'discordUser': interaction.user, 'typeName': type_name}, note='Linking existing forum channel failed.',
                )
                return await interaction.followup.send('Bot error saat menghubungkan jenis produk ke channel. Cek permission Manage Channels bot di channel tersebut.')

            await log_command_activity(
                interaction, subcommand='createtype', success=True,
                fields={'discordUser': interaction.user, 'typeName': type_name, 'forumChannel': result['forumChannel']},
            )

            note = (
                f'Jenis produk **{type_name}** sekarang terhubung ke {result["forumChannel"].mention}. Forum lama (kalau berbeda) tidak dihapus.'
                if result['wasExistingType']
                else f'Jenis produk **{type_name}** dibuat dan dihubungkan ke {result["forumChannel"].mention}.'
            )
            return await interaction.followup.send(note)

        try:
            result = await create_or_sync_product_type_forum(interaction.guild, str(interaction.guild_id), type_name)
        except Exception as err:
            print(f'create_or_sync_product_type_forum failed: {err}')
            await log_command_activity(
                interaction, subcommand='createtype', success=False,
                fields={'discordUser': interaction.user, 'typeName': type_name}, note='Forum channel creation/sync failed.',
            )
            return await interaction.followup.send('Bot error saat membuat/menyinkronkan forum jenis produk. Cek permission Manage Channels bot.')

        await log_command_activity(
            interaction, subcommand='createtype', success=True,
            fields={'discordUser': interaction.user, 'typeName': type_name, 'forumChannel': result['forumChannel']},
        )

        verb = 'dibuat' if result['created'] else 'disinkronkan ulang'
        await interaction.followup.send(f'Jenis produk **{type_name}** {verb}. Forum: {result["forumChannel"].mention}')

    @product_group.command(name='linktype', description='Link a product type to an already-existing forum channel')
    @app_commands.describe(nama='Nama jenis produk', channel='Existing forum channel to link')
    async def linktype(self, interaction: discord.Interaction, nama: str, channel: discord.ForumChannel):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        await interaction.response.defer(ephemeral=True)

        type_name = nama.strip()
        if not type_name:
            return await interaction.followup.send('Nama jenis tidak boleh kosong.')

        try:
            result = await link_existing_forum_to_type(interaction.guild, str(interaction.guild_id), type_name, channel)
        except Exception as err:
            print(f'link_existing_forum_to_type failed: {err}')
            await log_command_activity(
                interaction, subcommand='linktype', success=False,
                fields={'discordUser': interaction.user, 'typeName': type_name}, note='Linking existing forum channel failed.',
            )
            return await interaction.followup.send('Bot error saat menghubungkan jenis produk ke channel. Cek permission Manage Channels bot di channel tersebut.')

        await log_command_activity(
            interaction, subcommand='linktype', success=True,
            fields={'discordUser': interaction.user, 'typeName': type_name, 'forumChannel': result['forumChannel']},
        )

        note = (
            f'Jenis produk **{type_name}** sekarang terhubung ke {result["forumChannel"].mention}. Forum lama (kalau berbeda) tidak dihapus.'
            if result['wasExistingType']
            else f'Jenis produk **{type_name}** dibuat dan dihubungkan ke {result["forumChannel"].mention}.'
        )
        await interaction.followup.send(note)

    @product_group.command(name='sendpost', description="Post a product to its type's forum channel")
    @app_commands.describe(product_uuid='ID produk (UUID)')
    @app_commands.autocomplete(product_uuid=_autocomplete_any_product_uuid)
    async def sendpost(self, interaction: discord.Interaction, product_uuid: str):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        await interaction.response.defer(ephemeral=True)

        product_id = product_uuid.strip()
        product = await get_product(product_id)
        if not product:
            await log_command_activity(
                interaction, subcommand='sendpost', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id}, note='Product UUID not found.',
            )
            return await interaction.followup.send(
                f'Produk dengan ID `{product_id}` tidak ditemukan. Kalau produk ini baru dihapus, post forum-nya seharusnya sudah ikut terhapus lewat `/product delete`.'
            )

        if not product.get('typeForumId'):
            await log_command_activity(
                interaction, subcommand='sendpost', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id}, note='Product has no associated forum (type forum missing).',
            )
            return await interaction.followup.send(f"Produk ini belum punya forum jenis yang valid. Jalankan `/product createtype` untuk jenis **{product['type']}** dulu.")

        guild = interaction.guild
        forum_channel = guild.get_channel(int(product['typeForumId']))
        if forum_channel is None:
            try:
                forum_channel = await guild.fetch_channel(int(product['typeForumId']))
            except discord.HTTPException:
                forum_channel = None

        if not forum_channel or not isinstance(forum_channel, discord.ForumChannel):
            await log_command_activity(
                interaction, subcommand='sendpost', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id}, note='Forum channel missing or no longer a forum channel.',
            )
            return await interaction.followup.send('Forum untuk jenis produk ini sudah tidak ada. Jalankan `/product createtype` lagi untuk membuatnya ulang.')

        is_free = _is_free_product(product['price'])

        embed = discord.Embed(title=product['name'], color=0x00B0F4)
        embed.add_field(name='Harga', value='GRATIS' if is_free else product['price'], inline=True)
        embed.add_field(name='Jenis', value=product['type'], inline=True)
        embed.add_field(name='Kreator', value=product['creator'], inline=True)
        embed.add_field(name='Versi', value=f"v{product_version(product)}", inline=True)
        embed.add_field(name='Stok', value=stock_summary_text(product), inline=True)

        if product.get('reviewCount'):
            embed.add_field(name='Rating', value=f"⭐ {product['reviewAvg']}/10 ({product['reviewCount']} rating)", inline=True)
        testi_count = await get_testimony_count(str(interaction.guild_id))
        if testi_count:
            embed.add_field(name='Testimoni', value=str(testi_count), inline=True)

        if not is_free:
            embed.add_field(name='Link File', value=product['fileLink'], inline=False)

        review_media = product.get('reviewMedia') or ''
        if _IMAGE_URL_RE.search(review_media):
            embed.set_image(url=review_media)
        else:
            embed.add_field(name='Video/Gambar Review', value=review_media, inline=False)

        view = None
        if is_free:
            if _URL_RE.match(product.get('fileLink', '') or ''):
                view = discord.ui.View()
                view.add_item(discord.ui.Button(label='Download', style=discord.ButtonStyle.link, url=product['fileLink']))
            else:
                embed.add_field(name='Link File', value=product['fileLink'], inline=False)

        post_content = f"{product['description']}\n\nTutorial: {product['tutorialLink']}" if (is_free and product.get('tutorialLink')) else product['description']

        existing_thread = None
        if product.get('forumThreadId'):
            existing_thread = guild.get_thread(int(product['forumThreadId']))
            if existing_thread is None:
                try:
                    existing_thread = await guild.fetch_channel(int(product['forumThreadId']))
                except discord.HTTPException:
                    existing_thread = None

        thread = None
        was_update = False

        if existing_thread:
            try:
                if existing_thread.name != product['name']:
                    await existing_thread.edit(name=product['name'])
                starter_message = None
                try:
                    starter_message = existing_thread.starter_message or await existing_thread.fetch_message(existing_thread.id)
                except discord.HTTPException:
                    starter_message = None
                if not starter_message:
                    raise RuntimeError('Starter message missing, cannot edit in place.')
                await starter_message.edit(content=post_content, embed=embed, view=view)
                thread = existing_thread
                was_update = True
            except Exception as err:
                print(f'Failed to edit existing forum post in place, falling back to new thread: {err}')
                existing_thread = None

        if not existing_thread:
            try:
                thread_with_message = await forum_channel.create_thread(
                    name=product['name'], content=post_content, embed=embed, view=view,
                )
                thread = thread_with_message.thread
            except Exception as err:
                print(f'Forum post creation failed: {err}')
                await log_command_activity(
                    interaction, subcommand='sendpost', success=False,
                    fields={'discordUser': interaction.user, 'productId': product_id}, note='Bot error while creating forum post.',
                )
                return await interaction.followup.send('Bot error saat membuat post di forum. Cek permission bot di channel forum tersebut.')

        await log_command_activity(
            interaction, subcommand='sendpost', success=True,
            fields={'discordUser': interaction.user, 'productId': product_id, 'forumChannel': forum_channel},
            note='Updated existing post in place.' if was_update else 'Created new post.',
        )

        if not was_update:
            try:
                await save_product(product_id, {**product, 'forumThreadId': str(thread.id)})
            except Exception as err:
                print(f'Failed to save forumThreadId onto product (post itself succeeded): {err}')

        verb = 'diperbarui' if was_update else 'diposting'
        await interaction.followup.send(f'Produk **{product["name"]}** berhasil {verb}: {thread.mention}')

    @product_group.command(name='edit', description='Edit an existing product listing')
    @app_commands.describe(product_uuid='ID produk (UUID)')
    @app_commands.autocomplete(product_uuid=_autocomplete_any_product_uuid)
    async def edit(self, interaction: discord.Interaction, product_uuid: str):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        product_id = product_uuid.strip()
        product = await get_product(product_id)
        if not product:
            await log_command_activity(
                interaction, subcommand='edit', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id}, note='Product UUID not found.',
            )
            return await interaction.response.send_message(f'Produk dengan ID `{product_id}` tidak ditemukan.', ephemeral=True)

        await interaction.response.send_modal(EditModal1(interaction, product, product_id))

    @product_group.command(name='update', description='Bump a product to a new version with a changelog (stock/links untouched)')
    @app_commands.describe(product_uuid='ID produk (UUID)')
    @app_commands.autocomplete(product_uuid=_autocomplete_any_product_uuid)
    async def update(self, interaction: discord.Interaction, product_uuid: str):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        product_id = product_uuid.strip()
        product = await get_product(product_id)
        if not product:
            await log_command_activity(
                interaction, subcommand='update', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id}, note='Product UUID not found.',
            )
            return await interaction.response.send_message(f'Produk dengan ID `{product_id}` tidak ditemukan.', ephemeral=True)

        await interaction.response.send_modal(UpdateModal(interaction, product, product_id))

    @product_group.command(name='setstock', description='Add a stock batch (file link + quantity, or infinite) to a product')
    @app_commands.describe(product_uuid='ID produk (UUID)')
    @app_commands.autocomplete(product_uuid=_autocomplete_any_product_uuid)
    async def setstock(self, interaction: discord.Interaction, product_uuid: str):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        product_id = product_uuid.strip()
        product = await get_product(product_id)
        if not product:
            await log_command_activity(
                interaction, subcommand='setstock', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id}, note='Product UUID not found.',
            )
            return await interaction.response.send_message(f'Produk dengan ID `{product_id}` tidak ditemukan.', ephemeral=True)

        await interaction.response.send_modal(SetStockModal(interaction, product, product_id))

    @product_group.command(name='buy', description='Generate a QRIS payment for this order ticket and auto-deliver once paid')
    async def buy(self, interaction: discord.Interaction):
        if not await self._guild_check(interaction):
            return

        await interaction.response.defer()

        ticket = await get_ticket(str(interaction.channel_id))
        if not ticket or ticket.get('category') != 'order':
            return await interaction.followup.send('This command only works inside an order ticket. Open one from the ticket panel first.')

        if ticket.get('creatorId') != str(interaction.user.id):
            return await interaction.followup.send('Only the person who opened this ticket can run `/product buy` here.')

        if ticket.get('status') != 'open':
            return await interaction.followup.send('This ticket is already closed.')

        line_items = ticket.get('products') or []
        if len(line_items) != 1:
            return await interaction.followup.send(
                'Automatic QRIS checkout only supports a single product per ticket right now. '
                'Ask an admin to help with a multi-product order.'
            )

        if not pakasir_configured():
            return await interaction.followup.send('Payment gateway is not configured yet. Ask an admin to set `PAKASIR_PROJECT` / `PAKASIR_API_KEY`.')

        existing_order = await find_pending_order_for_channel(str(interaction.channel_id))
        if existing_order:
            return await interaction.followup.send(
                f"A payment is already pending for this ticket (expires {existing_order.get('expiredAt', 'soon')}). "
                f"Scan the QR code above, or wait for it to expire before running `/product buy` again."
            )

        item = line_items[0]
        product = await get_product(item['productId'])
        if not product:
            return await interaction.followup.send('That product no longer exists. Ask an admin to check this order.')

        if not is_in_stock(product):
            return await interaction.followup.send(f"**{product['name']}** is out of stock right now. Ask an admin to `/product setstock`.")

        amount = item.get('lineTotal') or _parse_price_local(item.get('price'))
        if amount <= 0:
            return await interaction.followup.send('This product is free -- ask an admin to `/product give` it to you directly instead.')

        try:
            order = await create_payment_order(
                guild_id=str(interaction.guild_id), channel_id=str(interaction.channel_id),
                buyer_id=str(interaction.user.id), product_id=product['id'],
                product_name=product['name'], amount=amount,
            )
        except PaymentGatewayError as err:
            print(f'[product buy] create_payment_order failed: {err}')
            await log_command_activity(
                interaction, subcommand='buy', success=False,
                fields={'discordUser': interaction.user, 'productId': product['id'], 'productName': product['name']},
                note='Pakasir transactioncreate failed.',
            )
            return await interaction.followup.send('Failed to generate a QRIS payment. Please try again in a moment.')

        qr_file = _render_qris_image(order['qrisString'], order['orderId'])

        embed = discord.Embed(title=f"Pay for {product['name']}", color=0x00B0F4)
        embed.add_field(name='Total', value=_format_idr_local(amount), inline=True)
        embed.add_field(name='Status', value='⏳ Menunggu pembayaran', inline=True)
        embed.set_footer(text='Scan pakai e-wallet apa saja yang support QRIS. Kadaluarsa dalam ~30 menit.')
        embed.set_image(url=f'attachment://{qr_file.filename}')

        view = QRISPaymentView(order_id=order['orderId'], product=product, buyer_id=str(interaction.user.id))
        message = await interaction.followup.send(embed=embed, file=qr_file, view=view, wait=True)
        view.message = message
        view.start_polling(interaction.client)

        await log_command_activity(
            interaction, subcommand='buy', success=True,
            fields={'discordUser': interaction.user, 'productId': product['id'], 'productName': product['name']},
            note=f"QRIS order {order['orderId']} created, amount {amount}.",
        )

    @product_group.command(name='view', description='Browse all products by type')
    async def view(self, interaction: discord.Interaction):
        if not await self._guild_check(interaction):
            return

        await interaction.response.defer()

        types = await list_product_types(str(interaction.guild_id))
        types.sort(key=lambda t: t['name'])

        if not types:
            return await interaction.followup.send('Belum ada jenis produk yang terdaftar.')

        await log_command_activity(interaction, subcommand='view', success=True, fields={'discordUser': interaction.user})

        view = ProductViewView(owner_id=interaction.user.id, guild_id=str(interaction.guild_id), types=types)
        embed = await view.build_embed()
        view._build_components()
        message = await interaction.followup.send(embed=embed, view=view, wait=True)
        view.message = message

    @product_group.command(name='delete', description='Delete a product')
    @app_commands.describe(product_uuid='ID produk (UUID)')
    @app_commands.autocomplete(product_uuid=_autocomplete_any_product_uuid)
    async def delete(self, interaction: discord.Interaction, product_uuid: str):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        product_id = product_uuid.strip()
        await interaction.response.send_modal(DeleteConfirmModal(interaction, product_id))

    @product_group.command(name='give', description='Give a product to a verified user')
    @app_commands.describe(user='Target user', product_uuid='ID produk (UUID)')
    @app_commands.autocomplete(product_uuid=_autocomplete_any_product_uuid)
    async def give(self, interaction: discord.Interaction, user: discord.User, product_uuid: str):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        await interaction.response.defer(ephemeral=True)

        product_id = product_uuid.strip()

        verified_record = await get_verified_user(str(user.id))
        if not verified_record:
            await log_command_activity(
                interaction, subcommand='give', success=False,
                fields={'discordUser': interaction.user, 'targetUser': user, 'productId': product_id}, note='Target user is not verified.',
            )
            return await interaction.followup.send(f'{user.mention} belum verifikasi. Suruh mereka jalankan `/verify start` dulu.')

        product = await get_product(product_id)
        if not product:
            await log_command_activity(
                interaction, subcommand='give', success=False,
                fields={'discordUser': interaction.user, 'targetUser': user, 'productId': product_id}, note='Product UUID not found.',
            )
            return await interaction.followup.send(f'Produk dengan ID `{product_id}` tidak ditemukan.')

        if user_owns_product(product, str(user.id)):
            await log_command_activity(
                interaction, subcommand='give', success=False,
                fields={'discordUser': interaction.user, 'targetUser': user, 'productId': product_id, 'productName': product['name']},
                note='Target user already owns this product.',
            )
            return await interaction.followup.send(f"{user.mention} sudah punya produk **{product['name']}**.")

        try:
            await give_product_to_user(product_id, str(user.id))
        except Exception as err:
            print(f'Failed to give product (Firestore write failed): {err}')
            await log_command_activity(
                interaction, subcommand='give', success=False,
                fields={'discordUser': interaction.user, 'targetUser': user, 'productId': product_id, 'productName': product['name']},
                note='Firestore write failed.',
            )
            return await interaction.followup.send('Gagal memberikan produk ke database. Coba lagi.')

        auto_note = ''
        try:
            granted_group_ids = await auto_whitelist_product_for_user(product_id, str(user.id))
            if granted_group_ids:
                auto_note = f" Otomatis di-whitelist ke group yang sudah ditautkan: {', '.join(f'`{g}`' for g in granted_group_ids)}."
        except Exception as err:
            print(f'auto_whitelist_product_for_user failed (product still given): {err}')
            auto_note = ' (Auto-whitelist ke group gagal, cek manual dengan /product groupwhitelist.)'

        await log_command_activity(
            interaction, subcommand='give', success=True,
            fields={'discordUser': interaction.user, 'targetUser': user, 'productId': product_id, 'productName': product['name']},
        )

        await interaction.followup.send(f"Produk **{product['name']}** berhasil diberikan ke {user.mention}.{auto_note}")

    @product_group.command(name='revoke', description='Revoke a product from a user')
    @app_commands.describe(user='Target user', product_uuid='ID produk (UUID)')
    @app_commands.autocomplete(product_uuid=_autocomplete_any_product_uuid)
    async def revoke(self, interaction: discord.Interaction, user: discord.User, product_uuid: str):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        await interaction.response.defer(ephemeral=True)

        product_id = product_uuid.strip()

        product = await get_product(product_id)
        if not product:
            await log_command_activity(
                interaction, subcommand='revoke', success=False,
                fields={'discordUser': interaction.user, 'targetUser': user, 'productId': product_id}, note='Product UUID not found.',
            )
            return await interaction.followup.send(f'Produk dengan ID `{product_id}` tidak ditemukan.')

        if not user_owns_product(product, str(user.id)):
            await log_command_activity(
                interaction, subcommand='revoke', success=False,
                fields={'discordUser': interaction.user, 'targetUser': user, 'productId': product_id, 'productName': product['name']},
                note='Target user does not own this product.',
            )
            return await interaction.followup.send(f"{user.mention} belum punya produk **{product['name']}**.")

        try:
            await revoke_product_from_user(product_id, str(user.id))
        except Exception as err:
            print(f'Failed to revoke product (Firestore write failed): {err}')
            await log_command_activity(
                interaction, subcommand='revoke', success=False,
                fields={'discordUser': interaction.user, 'targetUser': user, 'productId': product_id, 'productName': product['name']},
                note='Firestore write failed.',
            )
            return await interaction.followup.send('Gagal mencabut produk dari database. Coba lagi.')

        auto_note = ''
        try:
            removed_group_ids = await auto_revoke_product_for_user(product_id, str(user.id))
            if removed_group_ids:
                auto_note = f" Whitelist otomatis ke group juga dicabut: {', '.join(f'`{g}`' for g in removed_group_ids)}."
        except Exception as err:
            print(f'auto_revoke_product_for_user failed (product still revoked): {err}')
            auto_note = ' (Gagal auto-cabut whitelist group, cek manual dengan /product groupwhitelist.)'

        await log_command_activity(
            interaction, subcommand='revoke', success=True,
            fields={'discordUser': interaction.user, 'targetUser': user, 'productId': product_id, 'productName': product['name']},
        )

        await interaction.followup.send(f"Produk **{product['name']}** berhasil dicabut dari {user.mention}.{auto_note}")

    @product_group.command(name='groupwhitelist', description='Whitelist a product for use by ANY member of a Roblox group who has linked it')
    @app_commands.describe(product_uuid='ID produk (UUID)', group_id='Roblox group ID (numeric)', action='add or remove')
    @app_commands.choices(action=[
        app_commands.Choice(name='add', value='add'),
        app_commands.Choice(name='remove', value='remove'),
    ])
    @app_commands.autocomplete(product_uuid=_autocomplete_any_product_uuid)
    async def groupwhitelist(self, interaction: discord.Interaction, product_uuid: str, group_id: str, action: app_commands.Choice[str]):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        await interaction.response.defer(ephemeral=True)

        product_id = product_uuid.strip()
        group_id = group_id.strip()

        if not group_id.isdigit():
            return await interaction.followup.send('Group ID harus berupa angka.')

        product = await get_product(product_id)
        if not product:
            return await interaction.followup.send(f'Produk dengan ID `{product_id}` tidak ditemukan.')

        sub = 'groupwhitelistadd' if action.value == 'add' else 'groupwhitelistremove'

        if action.value == 'add':
            await add_group_whitelist(product_id, group_id)
            verb = 'sekarang bisa dipakai oleh'
        else:
            await remove_group_whitelist(product_id, group_id)
            verb = 'tidak lagi bisa dipakai oleh'

        await log_command_activity(
            interaction, subcommand=sub, success=True,
            fields={'discordUser': interaction.user, 'productId': product_id, 'groupId': group_id},
        )
        await interaction.followup.send(
            f"Produk **{product['name']}** {verb} anggota group `{group_id}` yang sudah `/verify linkgroup` "
            f"-- di place manapun yang dimiliki group itu, selama mereka masih anggotanya."
        )

    @product_group.command(name='get', description='Get the file link of a product you own, sent to your DM')
    @app_commands.describe(product_uuid='ID produk (UUID)')
    @app_commands.autocomplete(product_uuid=_autocomplete_get_product_uuid)
    async def get(self, interaction: discord.Interaction, product_uuid: str):
        await interaction.response.defer(ephemeral=True)

        product_id = product_uuid.strip()

        verified_record = await get_verified_user(str(interaction.user.id))
        if not verified_record:
            await log_command_activity(
                interaction, subcommand='get', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id}, note='Requesting user is not verified.',
            )
            return await interaction.followup.send('You are required to verified to use this command!')

        product = await get_product(product_id)
        if not product:
            await log_command_activity(
                interaction, subcommand='get', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id}, note='Product UUID not found.',
            )
            return await interaction.followup.send(f'Produk dengan ID `{product_id}` tidak ditemukan.')

        if not user_owns_product(product, str(interaction.user.id)):
            await log_command_activity(
                interaction, subcommand='get', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id, 'productName': product['name']},
                note='Requesting user does not own this product.',
            )
            return await interaction.followup.send('You didnt owned the product!')

        try:
            await interaction.user.send(**build_product_delivery_dm(product))
        except discord.HTTPException:
            await log_command_activity(
                interaction, subcommand='get', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id, 'productName': product['name']},
                note='Could not DM the user (DMs likely closed).',
            )
            return await interaction.followup.send('Could not DM you the file link. Please enable DMs from server members and try again.')

        await log_command_activity(
            interaction, subcommand='get', success=True,
            fields={'discordUser': interaction.user, 'productId': product_id, 'productName': product['name']},
        )

        await interaction.followup.send('Sent! Check your DMs 📬')

    @product_group.command(name='rating', description='Rate a product X/10 and say why -- posts in the product forum thread')
    @app_commands.describe(product_uuid='ID produk (UUID)')
    @app_commands.autocomplete(product_uuid=_autocomplete_get_product_uuid)
    async def rating(self, interaction: discord.Interaction, product_uuid: str):
        if not await self._guild_check(interaction):
            return

        product_id = product_uuid.strip()

        verified_record = await get_verified_user(str(interaction.user.id))
        if not verified_record:
            return await interaction.response.send_message('You are required to verified to use this command!', ephemeral=True)

        product = await get_product(product_id)
        if not product:
            await log_command_activity(
                interaction, subcommand='rating', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id}, note='Product UUID not found.',
            )
            return await interaction.response.send_message(f'Produk dengan ID `{product_id}` tidak ditemukan.', ephemeral=True)

        if not user_owns_product(product, str(interaction.user.id)):
            await log_command_activity(
                interaction, subcommand='rating', success=False,
                fields={'discordUser': interaction.user, 'productId': product_id, 'productName': product['name']},
                note='Requesting user does not own this product.',
            )
            return await interaction.response.send_message('You didnt owned the product!', ephemeral=True)

        await interaction.response.send_modal(RatingModal(interaction, product, product_id))

    @product_group.command(name='settesticount', description='[Admin] Manually set the displayed testimony count for this server')
    @app_commands.describe(count='New testimony count (whole number)')
    async def settesticount(self, interaction: discord.Interaction, count: app_commands.Range[int, 0, None]):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        await interaction.response.defer(ephemeral=True)

        try:
            await set_testimony_count(str(interaction.guild_id), count)
        except Exception as err:
            print(f'Failed to set testimonyCount: {err}')
            await log_command_activity(
                interaction, subcommand='settesticount', success=False,
                fields={'discordUser': interaction.user, 'count': count}, note='Firestore write failed.',
            )
            return await interaction.followup.send('Gagal mengubah testimony count di database. Coba lagi.')

        await log_command_activity(
            interaction, subcommand='settesticount', success=True,
            fields={'discordUser': interaction.user, 'count': count},
        )

        await interaction.followup.send(f'Testimony count server ini diset ke **{count}**.')

    @product_group.command(name='setreviewschannel', description='[Admin] Set the channel where product reviews get posted')
    @app_commands.describe(
        channel='Text channel where /product rating reviews will be posted',
        mod_role='Optional: role (besides Administrators) allowed to chat freely in that channel',
    )
    async def setreviewschannel(self, interaction: discord.Interaction, channel: discord.TextChannel, mod_role: discord.Role | None = None):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        await interaction.response.defer(ephemeral=True)

        try:
            await save_reviews_channel(str(interaction.guild_id), str(channel.id))
            await save_reviews_mod_role(str(interaction.guild_id), str(mod_role.id) if mod_role else None)
        except Exception as err:
            print(f'Failed to save reviews channel config: {err}')
            await log_command_activity(
                interaction, subcommand='setreviewschannel', success=False,
                fields={'discordUser': interaction.user}, note='Firestore write failed.',
            )
            return await interaction.followup.send('Gagal menyimpan pengaturan channel reviews. Coba lagi.')

        await log_command_activity(
            interaction, subcommand='setreviewschannel', success=True,
            fields={'discordUser': interaction.user, 'reviewsChannel': channel, 'modRole': mod_role},
        )

        role_note = f' Role **{mod_role.name}** juga dikecualikan dari cleanup.' if mod_role else ''
        await interaction.followup.send(
            f'Channel reviews diset ke {channel.mention}. Semua rating dari `/product rating` akan diposting di sana, '
            f'dan pesan dari member biasa di channel itu (selain menjalankan `/product rating`) akan otomatis dihapus.{role_note}'
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ProductCog(bot))
