import asyncio
import dataclasses
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from blspy import G1Element, G2Element, PrivateKey

from chia.consensus.block_rewards import calculate_base_farmer_reward
from chia.data_layer.data_layer_wallet import DataLayerWallet
from chia.pools.pool_wallet import PoolWallet
from chia.pools.pool_wallet_info import FARMING_TO_POOL, PoolState, PoolWalletInfo, create_pool_state
from chia.protocols.protocol_message_types import ProtocolMessageTypes
from chia.protocols.wallet_protocol import CoinState
from chia.rpc.rpc_server import Endpoint, EndpointResult, default_get_connections
from chia.server.outbound_message import NodeType, make_msg
from chia.server.ws_connection import WSChiaConnection
from chia.simulator.simulator_protocol import FarmNewBlockProtocol
from chia.types.announcement import Announcement
from chia.types.blockchain_format.coin import Coin, coin_as_list
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.types.spend_bundle import SpendBundle
from chia.util.bech32m import decode_puzzle_hash, encode_puzzle_hash
from chia.util.byte_types import hexstr_to_bytes
from chia.util.config import load_config
from chia.util.errors import KeychainIsLocked
from chia.util.ints import uint8, uint32, uint64, uint16
from chia.util.keychain import bytes_to_mnemonic, generate_mnemonic
from chia.util.path import path_from_root
from chia.util.ws_message import WsRpcMessage, create_payload_dict
from chia.wallet.cat_wallet.cat_constants import DEFAULT_CATS
from chia.wallet.cat_wallet.cat_wallet import CATWallet
from chia.wallet.derive_keys import (
    MAX_POOL_WALLETS,
    master_sk_to_farmer_sk,
    master_sk_to_pool_sk,
    master_sk_to_singleton_owner_sk,
    match_address_to_sk,
)
from chia.wallet.did_wallet.did_wallet import DIDWallet
from chia.wallet.nft_wallet import nft_puzzles
from chia.wallet.nft_wallet.nft_info import NFTInfo, NFTCoinInfo
from chia.wallet.nft_wallet.nft_puzzles import get_metadata_and_phs
from chia.wallet.nft_wallet.nft_wallet import NFTWallet
from chia.wallet.nft_wallet.uncurry_nft import UncurriedNFT
from chia.wallet.notification_store import Notification
from chia.wallet.outer_puzzles import AssetType
from chia.wallet.puzzle_drivers import PuzzleInfo, Solver
from chia.wallet.trade_record import TradeRecord
from chia.wallet.trading.offer import Offer
from chia.wallet.transaction_record import TransactionRecord
from chia.wallet.util.address_type import AddressType, is_valid_address
from chia.wallet.util.transaction_type import TransactionType
from chia.wallet.util.wallet_types import AmountWithPuzzlehash, WalletType
from chia.wallet.wallet_info import WalletInfo
from chia.wallet.wallet_node import WalletNode
from chia.wallet.wallet import Wallet
from chia.wallet.wallet_protocol import WalletProtocol

# Timeout for response from wallet/full node for sending a transaction
TIMEOUT = 30
MAX_DERIVATION_INDEX_DELTA = 1000

log = logging.getLogger(__name__)


class WalletRpcApi:
    def __init__(self, wallet_node: WalletNode):
        assert wallet_node is not None
        self.service = wallet_node
        self.service_name = "chia_wallet"
        self.balance_cache: Dict[int, Any] = {}

    def get_routes(self) -> Dict[str, Endpoint]:
        return {
            # Key management
            "/log_in": self.log_in,
            "/get_logged_in_fingerprint": self.get_logged_in_fingerprint,
            "/get_public_keys": self.get_public_keys,
            "/get_private_key": self.get_private_key,
            "/generate_mnemonic": self.generate_mnemonic,
            "/add_key": self.add_key,
            "/delete_key": self.delete_key,
            "/check_delete_key": self.check_delete_key,
            "/delete_all_keys": self.delete_all_keys,
            # Wallet node
            "/get_sync_status": self.get_sync_status,
            "/get_height_info": self.get_height_info,
            "/push_tx": self.push_tx,
            "/push_transactions": self.push_transactions,
            "/farm_block": self.farm_block,  # Only when node simulator is running
            # this function is just here for backwards-compatibility. It will probably
            # be removed in the future
            "/get_initial_freeze_period": self.get_initial_freeze_period,
            "/get_network_info": self.get_network_info,
            # Wallet management
            "/get_wallets": self.get_wallets,
            "/create_new_wallet": self.create_new_wallet,
            # Wallet
            "/get_wallet_balance": self.get_wallet_balance,
            "/get_transaction": self.get_transaction,
            "/get_transactions": self.get_transactions,
            "/get_transaction_count": self.get_transaction_count,
            "/get_next_address": self.get_next_address,
            "/send_transaction": self.send_transaction,
            "/send_transaction_multi": self.send_transaction_multi,
            "/get_farmed_amount": self.get_farmed_amount,
            "/create_signed_transaction": self.create_signed_transaction,
            "/delete_unconfirmed_transactions": self.delete_unconfirmed_transactions,
            "/select_coins": self.select_coins,
            "/get_current_derivation_index": self.get_current_derivation_index,
            "/extend_derivation_index": self.extend_derivation_index,
            "/get_notifications": self.get_notifications,
            "/delete_notifications": self.delete_notifications,
            "/send_notification": self.send_notification,
            "/sign_message_by_address": self.sign_message_by_address,
            "/sign_message_by_id": self.sign_message_by_id,
            # CATs and trading
            "/cat_set_name": self.cat_set_name,
            "/cat_asset_id_to_name": self.cat_asset_id_to_name,
            "/cat_get_name": self.cat_get_name,
            "/get_stray_cats": self.get_stray_cats,
            "/cat_spend": self.cat_spend,
            "/cat_get_asset_id": self.cat_get_asset_id,
            "/create_offer_for_ids": self.create_offer_for_ids,
            "/get_offer_summary": self.get_offer_summary,
            "/check_offer_validity": self.check_offer_validity,
            "/take_offer": self.take_offer,
            "/get_offer": self.get_offer,
            "/get_all_offers": self.get_all_offers,
            "/get_offers_count": self.get_offers_count,
            "/cancel_offer": self.cancel_offer,
            "/cancel_offers": self.cancel_offers,
            "/get_cat_list": self.get_cat_list,
            # DID Wallet
            "/did_set_wallet_name": self.did_set_wallet_name,
            "/did_get_wallet_name": self.did_get_wallet_name,
            "/did_update_recovery_ids": self.did_update_recovery_ids,
            "/did_update_metadata": self.did_update_metadata,
            "/did_get_pubkey": self.did_get_pubkey,
            "/did_get_did": self.did_get_did,
            "/did_recovery_spend": self.did_recovery_spend,
            "/did_get_recovery_list": self.did_get_recovery_list,
            "/did_get_metadata": self.did_get_metadata,
            "/did_create_attest": self.did_create_attest,
            "/did_get_information_needed_for_recovery": self.did_get_information_needed_for_recovery,
            "/did_get_current_coin_info": self.did_get_current_coin_info,
            "/did_create_backup_file": self.did_create_backup_file,
            "/did_transfer_did": self.did_transfer_did,
            # NFT Wallet
            "/nft_mint_nft": self.nft_mint_nft,
            "/nft_get_nfts": self.nft_get_nfts,
            "/nft_get_by_did": self.nft_get_by_did,
            "/nft_set_nft_did": self.nft_set_nft_did,
            "/nft_set_nft_status": self.nft_set_nft_status,
            "/nft_get_wallet_did": self.nft_get_wallet_did,
            "/nft_get_wallets_with_dids": self.nft_get_wallets_with_dids,
            "/nft_get_info": self.nft_get_info,
            "/nft_transfer_nft": self.nft_transfer_nft,
            "/nft_add_uri": self.nft_add_uri,
            "/nft_calculate_royalties": self.nft_calculate_royalties,
            "/nft_mint_bulk": self.nft_mint_bulk,
            # Pool Wallet
            "/pw_join_pool": self.pw_join_pool,
            "/pw_self_pool": self.pw_self_pool,
            "/pw_absorb_rewards": self.pw_absorb_rewards,
            "/pw_status": self.pw_status,
            # DL Wallet
            "/create_new_dl": self.create_new_dl,
            "/dl_track_new": self.dl_track_new,
            "/dl_stop_tracking": self.dl_stop_tracking,
            "/dl_latest_singleton": self.dl_latest_singleton,
            "/dl_singletons_by_root": self.dl_singletons_by_root,
            "/dl_update_root": self.dl_update_root,
            "/dl_update_multiple": self.dl_update_multiple,
            "/dl_history": self.dl_history,
            "/dl_owned_singletons": self.dl_owned_singletons,
            "/dl_get_mirrors": self.dl_get_mirrors,
            "/dl_new_mirror": self.dl_new_mirror,
            "/dl_delete_mirror": self.dl_delete_mirror,
        }

    def get_connections(self, request_node_type: Optional[NodeType]) -> List[Dict[str, Any]]:
        return default_get_connections(server=self.service.server, request_node_type=request_node_type)

    async def _state_changed(self, change: str, change_data: Optional[Dict[str, Any]]) -> List[WsRpcMessage]:
        """
        Called by the WalletNode or WalletStateManager when something has changed in the wallet. This
        gives us an opportunity to send notifications to all connected clients via WebSocket.
        """
        payloads = []
        if change in {"sync_changed", "coin_added"}:
            # Metrics is the only current consumer for this event
            payloads.append(create_payload_dict(change, change_data, self.service_name, "metrics"))

        if change in {
            "offer_cancelled",
            "offer_added",
            "wallet_created",
            "did_coin_added",
            "nft_coin_added",
            "nft_coin_removed",
            "nft_coin_updated",
            "nft_coin_did_set",
            "new_block",
            "coin_removed",
            "coin_added",
            "new_derivation_index",
            "added_stray_cat",
            "pending_transaction",
            "tx_update",
        }:
            payloads.append(create_payload_dict("state_changed", change_data, self.service_name, "wallet_ui"))

        return payloads

    async def _stop_wallet(self):
        """
        Stops a currently running wallet/key, which allows starting the wallet with a new key.
        Each key has it's own wallet database.
        """
        if self.service is not None:
            self.service._close()
            peers_close_task: Optional[asyncio.Task] = await self.service._await_closed(shutting_down=False)
            if peers_close_task is not None:
                await peers_close_task

    async def _convert_tx_puzzle_hash(self, tx: TransactionRecord) -> TransactionRecord:
        return dataclasses.replace(
            tx,
            to_puzzle_hash=(
                await self.service.wallet_state_manager.convert_puzzle_hash(tx.wallet_id, tx.to_puzzle_hash)
            ),
        )

    ##########################################################################################
    # Key management
    ##########################################################################################

    async def log_in(self, request) -> EndpointResult:
        """
        Logs in the wallet with a specific key.
        """

        fingerprint = request["fingerprint"]
        if self.service.logged_in_fingerprint == fingerprint:
            return {"fingerprint": fingerprint}

        await self._stop_wallet()
        self.balance_cache = {}
        started = await self.service._start_with_fingerprint(fingerprint)
        if started is True:
            return {"fingerprint": fingerprint}

        return {"success": False, "error": "Unknown Error"}

    async def get_logged_in_fingerprint(self, request: Dict) -> EndpointResult:
        return {"fingerprint": self.service.logged_in_fingerprint}

    async def get_public_keys(self, request: Dict) -> EndpointResult:
        try:
            fingerprints = [
                sk.get_g1().get_fingerprint() for (sk, seed) in await self.service.keychain_proxy.get_all_private_keys()
            ]
        except KeychainIsLocked:
            return {"keyring_is_locked": True}
        except Exception as e:
            raise Exception(
                "Error while getting keys.  If the issue persists, restart all services."
                f"  Original error: {type(e).__name__}: {e}"
            ) from e
        else:
            return {"public_key_fingerprints": fingerprints}

    async def _get_private_key(self, fingerprint) -> Tuple[Optional[PrivateKey], Optional[bytes]]:
        try:
            all_keys = await self.service.keychain_proxy.get_all_private_keys()
            for sk, seed in all_keys:
                if sk.get_g1().get_fingerprint() == fingerprint:
                    return sk, seed
        except Exception as e:
            log.error(f"Failed to get private key by fingerprint: {e}")
        return None, None

    async def get_private_key(self, request) -> EndpointResult:
        fingerprint = request["fingerprint"]
        sk, seed = await self._get_private_key(fingerprint)
        if sk is not None:
            s = bytes_to_mnemonic(seed) if seed is not None else None
            return {
                "private_key": {
                    "fingerprint": fingerprint,
                    "sk": bytes(sk).hex(),
                    "pk": bytes(sk.get_g1()).hex(),
                    "farmer_pk": bytes(master_sk_to_farmer_sk(sk).get_g1()).hex(),
                    "pool_pk": bytes(master_sk_to_pool_sk(sk).get_g1()).hex(),
                    "seed": s,
                },
            }
        return {"success": False, "private_key": {"fingerprint": fingerprint}}

    async def generate_mnemonic(self, request: Dict) -> EndpointResult:
        return {"mnemonic": generate_mnemonic().split(" ")}

    async def add_key(self, request) -> EndpointResult:
        if "mnemonic" not in request:
            raise ValueError("Mnemonic not in request")

        # Adding a key from 24 word mnemonic
        mnemonic = request["mnemonic"]
        try:
            sk = await self.service.keychain_proxy.add_private_key(" ".join(mnemonic))
        except KeyError as e:
            return {
                "success": False,
                "error": f"The word '{e.args[0]}' is incorrect.'",
                "word": e.args[0],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

        fingerprint = sk.get_g1().get_fingerprint()
        await self._stop_wallet()

        # Makes sure the new key is added to config properly
        started = False
        try:
            await self.service.keychain_proxy.check_keys(self.service.root_path)
        except Exception as e:
            log.error(f"Failed to check_keys after adding a new key: {e}")
        started = await self.service._start_with_fingerprint(fingerprint=fingerprint)
        if started is True:
            return {"fingerprint": fingerprint}
        raise ValueError("Failed to start")

    async def delete_key(self, request) -> EndpointResult:
        await self._stop_wallet()
        fingerprint = request["fingerprint"]
        try:
            await self.service.keychain_proxy.delete_key_by_fingerprint(fingerprint)
        except Exception as e:
            log.error(f"Failed to delete key by fingerprint: {e}")
            return {"success": False, "error": str(e)}
        path = path_from_root(
            self.service.root_path,
            f"{self.service.config['database_path']}-{fingerprint}",
        )
        if path.exists():
            path.unlink()
        return {}

    async def _check_key_used_for_rewards(
        self, new_root: Path, sk: PrivateKey, max_ph_to_search: int
    ) -> Tuple[bool, bool]:
        """Checks if the given key is used for either the farmer rewards or pool rewards
        returns a tuple of two booleans
        The first is true if the key is used as the Farmer rewards, otherwise false
        The second is true if the key is used as the Pool rewards, otherwise false
        Returns both false if the key cannot be found with the given fingerprint
        """
        if sk is None:
            return False, False

        config: Dict = load_config(new_root, "config.yaml")
        farmer_target = config["farmer"].get("xch_target_address")
        pool_target = config["pool"].get("xch_target_address")
        address_to_check: List[bytes32] = [decode_puzzle_hash(farmer_target), decode_puzzle_hash(pool_target)]

        found_addresses: Set[bytes32] = match_address_to_sk(sk, address_to_check, max_ph_to_search)

        found_farmer = address_to_check[0] in found_addresses
        found_pool = address_to_check[1] in found_addresses

        return found_farmer, found_pool

    async def check_delete_key(self, request) -> EndpointResult:
        """Check the key use prior to possible deletion
        checks whether key is used for either farm or pool rewards
        checks if any wallets have a non-zero balance
        """
        used_for_farmer: bool = False
        used_for_pool: bool = False
        walletBalance: bool = False

        fingerprint = request["fingerprint"]
        max_ph_to_search = request.get("max_ph_to_search", 100)
        sk, _ = await self._get_private_key(fingerprint)
        if sk is not None:
            used_for_farmer, used_for_pool = await self._check_key_used_for_rewards(
                self.service.root_path, sk, max_ph_to_search
            )

            if self.service.logged_in_fingerprint != fingerprint:
                await self._stop_wallet()
                await self.service._start_with_fingerprint(fingerprint=fingerprint)

            wallets: List[WalletInfo] = await self.service.wallet_state_manager.get_all_wallet_info_entries()
            for w in wallets:
                wallet = self.service.wallet_state_manager.wallets[w.id]
                unspent = await self.service.wallet_state_manager.coin_store.get_unspent_coins_for_wallet(w.id)
                balance = await wallet.get_confirmed_balance(unspent)
                pending_balance = await wallet.get_unconfirmed_balance(unspent)

                if (balance + pending_balance) > 0:
                    walletBalance = True
                    break

        return {
            "fingerprint": fingerprint,
            "used_for_farmer_rewards": used_for_farmer,
            "used_for_pool_rewards": used_for_pool,
            "wallet_balance": walletBalance,
        }

    async def delete_all_keys(self, request: Dict) -> EndpointResult:
        await self._stop_wallet()
        try:
            await self.service.keychain_proxy.delete_all_keys()
        except Exception as e:
            log.error(f"Failed to delete all keys: {e}")
            return {"success": False, "error": str(e)}
        path = path_from_root(self.service.root_path, self.service.config["database_path"])
        if path.exists():
            path.unlink()
        return {}

    ##########################################################################################
    # Wallet Node
    ##########################################################################################

    async def get_sync_status(self, request: Dict) -> EndpointResult:
        sync_mode = self.service.wallet_state_manager.sync_mode
        has_pending_queue_items = self.service.new_peak_queue.has_pending_data_process_items()
        syncing = sync_mode or has_pending_queue_items
        synced = await self.service.wallet_state_manager.synced()
        return {"synced": synced, "syncing": syncing, "genesis_initialized": True}

    async def get_height_info(self, request: Dict) -> EndpointResult:
        height = await self.service.wallet_state_manager.blockchain.get_finished_sync_up_to()
        return {"height": height}

    async def get_network_info(self, request: Dict) -> EndpointResult:
        network_name = self.service.config["selected_network"]
        address_prefix = self.service.config["network_overrides"]["config"][network_name]["address_prefix"]
        return {"network_name": network_name, "network_prefix": address_prefix}

    async def push_tx(self, request: Dict) -> EndpointResult:
        nodes = self.service.server.get_connections(NodeType.FULL_NODE)
        if len(nodes) == 0:
            raise ValueError("Wallet is not currently connected to any full node peers")
        await self.service.push_tx(SpendBundle.from_bytes(hexstr_to_bytes(request["spend_bundle"])))
        return {}

    async def push_transactions(self, request: Dict) -> EndpointResult:
        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced before sending transactions")

        wallet = self.service.wallet_state_manager.main_wallet

        txs: List[TransactionRecord] = []
        for transaction_hexstr in request["transactions"]:
            tx = TransactionRecord.from_bytes(hexstr_to_bytes(transaction_hexstr))
            txs.append(tx)

        async with self.service.wallet_state_manager.lock:
            for tx in txs:
                await wallet.push_transaction(tx)

        return {}

    async def farm_block(self, request) -> EndpointResult:
        raw_puzzle_hash = decode_puzzle_hash(request["address"])
        request = FarmNewBlockProtocol(raw_puzzle_hash)
        msg = make_msg(ProtocolMessageTypes.farm_new_block, request)

        await self.service.server.send_to_all([msg], NodeType.FULL_NODE)
        return {}

    ##########################################################################################
    # Wallet Management
    ##########################################################################################

    async def get_wallets(self, request: Dict) -> EndpointResult:
        include_data: bool = request.get("include_data", True)
        wallet_type: Optional[WalletType] = None
        if "type" in request:
            wallet_type = WalletType(request["type"])

        wallets: List[WalletInfo] = await self.service.wallet_state_manager.get_all_wallet_info_entries(wallet_type)
        if not include_data:
            result: List[WalletInfo] = []
            for wallet in wallets:
                result.append(WalletInfo(wallet.id, wallet.name, wallet.type, ""))
            wallets = result
        response: EndpointResult = {"wallets": wallets}
        if self.service.logged_in_fingerprint is not None:
            response["fingerprint"] = self.service.logged_in_fingerprint
        return response

    async def create_new_wallet(self, request: Dict) -> EndpointResult:
        wallet_state_manager = self.service.wallet_state_manager

        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced.")
        main_wallet = wallet_state_manager.main_wallet
        fee = uint64(request.get("fee", 0))

        if request["wallet_type"] == "cat_wallet":
            # If not provided, the name will be autogenerated based on the tail hash.
            name = request.get("name", None)
            if request["mode"] == "new":
                async with self.service.wallet_state_manager.lock:
                    cat_wallet: CATWallet = await CATWallet.create_new_cat_wallet(
                        wallet_state_manager,
                        main_wallet,
                        {"identifier": "genesis_by_id"},
                        uint64(request["amount"]),
                        name,
                    )
                    asset_id = cat_wallet.get_asset_id()
                self.service.wallet_state_manager.state_changed("wallet_created")
                return {"type": cat_wallet.type(), "asset_id": asset_id, "wallet_id": cat_wallet.id()}

            elif request["mode"] == "existing":
                async with self.service.wallet_state_manager.lock:
                    cat_wallet = await CATWallet.create_wallet_for_cat(
                        wallet_state_manager, main_wallet, request["asset_id"], name
                    )
                self.service.wallet_state_manager.state_changed("wallet_created")
                return {"type": cat_wallet.type(), "asset_id": request["asset_id"], "wallet_id": cat_wallet.id()}

            else:  # undefined mode
                pass

        elif request["wallet_type"] == "did_wallet":
            if request["did_type"] == "new":
                backup_dids = []
                num_needed = 0
                for d in request["backup_dids"]:
                    backup_dids.append(decode_puzzle_hash(d))
                if len(backup_dids) > 0:
                    num_needed = uint64(request["num_of_backup_ids_needed"])
                metadata: Dict[str, str] = {}
                if "metadata" in request:
                    if type(request["metadata"]) is dict:
                        metadata = request["metadata"]

                async with self.service.wallet_state_manager.lock:
                    did_wallet_name: str = request.get("wallet_name", None)
                    if did_wallet_name is not None:
                        did_wallet_name = did_wallet_name.strip()
                    did_wallet: DIDWallet = await DIDWallet.create_new_did_wallet(
                        wallet_state_manager,
                        main_wallet,
                        uint64(request["amount"]),
                        backup_dids,
                        uint64(num_needed),
                        metadata,
                        did_wallet_name,
                        uint64(request.get("fee", 0)),
                    )

                    my_did_id = encode_puzzle_hash(
                        bytes32.fromhex(did_wallet.get_my_DID()), AddressType.DID.hrp(self.service.config)
                    )
                    nft_wallet_name = did_wallet_name
                    if nft_wallet_name is not None:
                        nft_wallet_name = f"{nft_wallet_name} NFT Wallet"
                    await NFTWallet.create_new_nft_wallet(
                        wallet_state_manager,
                        main_wallet,
                        bytes32.fromhex(did_wallet.get_my_DID()),
                        nft_wallet_name,
                    )
                return {
                    "success": True,
                    "type": did_wallet.type(),
                    "my_did": my_did_id,
                    "wallet_id": did_wallet.id(),
                }

            elif request["did_type"] == "recovery":
                async with self.service.wallet_state_manager.lock:
                    did_wallet = await DIDWallet.create_new_did_wallet_from_recovery(
                        wallet_state_manager, main_wallet, request["backup_data"]
                    )
                assert did_wallet.did_info.temp_coin is not None
                assert did_wallet.did_info.temp_puzhash is not None
                assert did_wallet.did_info.temp_pubkey is not None
                my_did = did_wallet.get_my_DID()
                coin_name = did_wallet.did_info.temp_coin.name().hex()
                coin_list = coin_as_list(did_wallet.did_info.temp_coin)
                newpuzhash = did_wallet.did_info.temp_puzhash
                pubkey = did_wallet.did_info.temp_pubkey
                return {
                    "success": True,
                    "type": did_wallet.type(),
                    "my_did": my_did,
                    "wallet_id": did_wallet.id(),
                    "coin_name": coin_name,
                    "coin_list": coin_list,
                    "newpuzhash": newpuzhash.hex(),
                    "pubkey": pubkey.hex(),
                    "backup_dids": did_wallet.did_info.backup_ids,
                    "num_verifications_required": did_wallet.did_info.num_of_backup_ids_needed,
                }
            else:  # undefined did_type
                pass
        elif request["wallet_type"] == "nft_wallet":
            for wallet in self.service.wallet_state_manager.wallets.values():
                did_id: Optional[bytes32] = None
                if "did_id" in request and request["did_id"] is not None:
                    did_id = decode_puzzle_hash(request["did_id"])
                if wallet.type() == WalletType.NFT:
                    assert isinstance(wallet, NFTWallet)
                    if wallet.get_did() == did_id:
                        log.info("NFT wallet already existed, skipping.")
                        return {
                            "success": True,
                            "type": wallet.type(),
                            "wallet_id": wallet.id(),
                        }

            async with self.service.wallet_state_manager.lock:
                nft_wallet: NFTWallet = await NFTWallet.create_new_nft_wallet(
                    wallet_state_manager, main_wallet, did_id, request.get("name", None)
                )
            return {
                "success": True,
                "type": nft_wallet.type(),
                "wallet_id": nft_wallet.id(),
            }
        elif request["wallet_type"] == "pool_wallet":
            if request["mode"] == "new":
                if "initial_target_state" not in request:
                    raise AttributeError("Daemon didn't send `initial_target_state`. Try updating the daemon.")

                owner_puzzle_hash: bytes32 = await self.service.wallet_state_manager.main_wallet.get_puzzle_hash(True)

                from chia.pools.pool_wallet_info import initial_pool_state_from_dict

                async with self.service.wallet_state_manager.lock:
                    # We assign a pseudo unique id to each pool wallet, so that each one gets its own deterministic
                    # owner and auth keys. The public keys will go on the blockchain, and the private keys can be found
                    # using the root SK and trying each index from zero. The indexes are not fully unique though,
                    # because the PoolWallet is not created until the tx gets confirmed on chain. Therefore if we
                    # make multiple pool wallets at the same time, they will have the same ID.
                    max_pwi = 1
                    for _, wallet in self.service.wallet_state_manager.wallets.items():
                        if wallet.type() == WalletType.POOLING_WALLET:
                            assert isinstance(wallet, PoolWallet)
                            pool_wallet_index = await wallet.get_pool_wallet_index()
                            if pool_wallet_index > max_pwi:
                                max_pwi = pool_wallet_index

                    if max_pwi + 1 >= (MAX_POOL_WALLETS - 1):
                        raise ValueError(f"Too many pool wallets ({max_pwi}), cannot create any more on this key.")

                    owner_sk: PrivateKey = master_sk_to_singleton_owner_sk(
                        self.service.wallet_state_manager.private_key, uint32(max_pwi + 1)
                    )
                    owner_pk: G1Element = owner_sk.get_g1()

                    initial_target_state = initial_pool_state_from_dict(
                        request["initial_target_state"], owner_pk, owner_puzzle_hash
                    )
                    assert initial_target_state is not None

                    try:
                        delayed_address = None
                        if "p2_singleton_delayed_ph" in request:
                            delayed_address = bytes32.from_hexstr(request["p2_singleton_delayed_ph"])
                        tr, p2_singleton_puzzle_hash, launcher_id = await PoolWallet.create_new_pool_wallet_transaction(
                            wallet_state_manager,
                            main_wallet,
                            initial_target_state,
                            fee,
                            request.get("p2_singleton_delay_time", None),
                            delayed_address,
                        )
                    except Exception as e:
                        raise ValueError(str(e))
                    return {
                        "total_fee": fee * 2,
                        "transaction": tr,
                        "launcher_id": launcher_id.hex(),
                        "p2_singleton_puzzle_hash": p2_singleton_puzzle_hash.hex(),
                    }
            elif request["mode"] == "recovery":
                raise ValueError("Need upgraded singleton for on-chain recovery")

        else:  # undefined wallet_type
            pass

        # TODO: rework this function to report detailed errors for each error case
        return {"success": False, "error": "invalid request"}

    ##########################################################################################
    # Wallet
    ##########################################################################################

    async def get_wallet_balance(self, request: Dict) -> EndpointResult:
        wallet_id = uint32(int(request["wallet_id"]))
        wallet = self.service.wallet_state_manager.wallets[wallet_id]

        # If syncing return the last available info or 0s
        syncing = self.service.wallet_state_manager.sync_mode
        if syncing:
            if wallet_id in self.balance_cache:
                wallet_balance = self.balance_cache[wallet_id]
            else:
                wallet_balance = {
                    "wallet_id": wallet_id,
                    "confirmed_wallet_balance": 0,
                    "unconfirmed_wallet_balance": 0,
                    "spendable_balance": 0,
                    "pending_change": 0,
                    "max_send_amount": 0,
                    "unspent_coin_count": 0,
                    "pending_coin_removal_count": 0,
                    "wallet_type": wallet.type(),
                }
                if self.service.logged_in_fingerprint is not None:
                    wallet_balance["fingerprint"] = self.service.logged_in_fingerprint
                if wallet.type() == WalletType.CAT:
                    assert isinstance(wallet, CATWallet)
                    wallet_balance["asset_id"] = wallet.get_asset_id()
        else:
            async with self.service.wallet_state_manager.lock:
                unspent_records = await self.service.wallet_state_manager.coin_store.get_unspent_coins_for_wallet(
                    wallet_id
                )
                balance = await wallet.get_confirmed_balance(unspent_records)
                pending_balance = await wallet.get_unconfirmed_balance(unspent_records)
                spendable_balance = await wallet.get_spendable_balance(unspent_records)
                pending_change = await wallet.get_pending_change_balance()
                max_send_amount = await wallet.get_max_send_amount(unspent_records)

                unconfirmed_removals: Dict[
                    bytes32, Coin
                ] = await wallet.wallet_state_manager.unconfirmed_removals_for_wallet(wallet_id)
                wallet_balance = {
                    "wallet_id": wallet_id,
                    "confirmed_wallet_balance": balance,
                    "unconfirmed_wallet_balance": pending_balance,
                    "spendable_balance": spendable_balance,
                    "pending_change": pending_change,
                    "max_send_amount": max_send_amount,
                    "unspent_coin_count": len(unspent_records),
                    "pending_coin_removal_count": len(unconfirmed_removals),
                    "wallet_type": wallet.type(),
                }
                if self.service.logged_in_fingerprint is not None:
                    wallet_balance["fingerprint"] = self.service.logged_in_fingerprint
                if wallet.type() == WalletType.CAT:
                    assert isinstance(wallet, CATWallet)
                    wallet_balance["asset_id"] = wallet.get_asset_id()
                self.balance_cache[wallet_id] = wallet_balance

        return {"wallet_balance": wallet_balance}

    async def get_transaction(self, request: Dict) -> EndpointResult:
        transaction_id: bytes32 = bytes32(hexstr_to_bytes(request["transaction_id"]))
        tr: Optional[TransactionRecord] = await self.service.wallet_state_manager.get_transaction(transaction_id)
        if tr is None:
            raise ValueError(f"Transaction 0x{transaction_id.hex()} not found")

        return {
            "transaction": (await self._convert_tx_puzzle_hash(tr)).to_json_dict_convenience(self.service.config),
            "transaction_id": tr.name,
        }

    async def get_transactions(self, request: Dict) -> EndpointResult:
        wallet_id = int(request["wallet_id"])

        start = request.get("start", 0)
        end = request.get("end", 50)
        sort_key = request.get("sort_key", None)
        reverse = request.get("reverse", False)

        to_address = request.get("to_address", None)
        to_puzzle_hash: Optional[bytes32] = None
        if to_address is not None:
            to_puzzle_hash = decode_puzzle_hash(to_address)

        transactions = await self.service.wallet_state_manager.tx_store.get_transactions_between(
            wallet_id, start, end, sort_key=sort_key, reverse=reverse, to_puzzle_hash=to_puzzle_hash
        )
        return {
            "transactions": [
                (await self._convert_tx_puzzle_hash(tr)).to_json_dict_convenience(self.service.config)
                for tr in transactions
            ],
            "wallet_id": wallet_id,
        }

    async def get_transaction_count(self, request: Dict) -> EndpointResult:
        wallet_id = int(request["wallet_id"])
        count = await self.service.wallet_state_manager.tx_store.get_transaction_count_for_wallet(wallet_id)
        return {
            "count": count,
            "wallet_id": wallet_id,
        }

    # this function is just here for backwards-compatibility. It will probably
    # be removed in the future
    async def get_initial_freeze_period(self, _: Dict) -> EndpointResult:
        # Mon May 03 2021 17:00:00 GMT+0000
        return {"INITIAL_FREEZE_END_TIMESTAMP": 1620061200}

    async def get_next_address(self, request: Dict) -> EndpointResult:
        """
        Returns a new address
        """
        if request["new_address"] is True:
            create_new = True
        else:
            create_new = False
        wallet_id = uint32(int(request["wallet_id"]))
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        selected = self.service.config["selected_network"]
        prefix = self.service.config["network_overrides"]["config"][selected]["address_prefix"]
        if wallet.type() == WalletType.STANDARD_WALLET:
            assert isinstance(wallet, Wallet)
            raw_puzzle_hash = await wallet.get_puzzle_hash(create_new)
            address = encode_puzzle_hash(raw_puzzle_hash, prefix)
        elif wallet.type() == WalletType.CAT:
            assert isinstance(wallet, CATWallet)
            raw_puzzle_hash = await wallet.standard_wallet.get_puzzle_hash(create_new)
            address = encode_puzzle_hash(raw_puzzle_hash, prefix)
        else:
            raise ValueError(f"Wallet type {wallet.type()} cannot create puzzle hashes")

        return {
            "wallet_id": wallet_id,
            "address": address,
        }

    async def send_transaction(self, request) -> EndpointResult:
        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced before sending transactions")

        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]

        if wallet.type() != WalletType.STANDARD_WALLET:
            raise ValueError("send_transaction only works for standard wallets")

        assert isinstance(wallet, Wallet)

        if not isinstance(request["amount"], int) or not isinstance(request["fee"], int):
            raise ValueError("An integer amount or fee is required (too many decimals)")
        amount: uint64 = uint64(request["amount"])
        address = request["address"]
        selected_network = self.service.config["selected_network"]
        expected_prefix = self.service.config["network_overrides"]["config"][selected_network]["address_prefix"]
        if address[0 : len(expected_prefix)] != expected_prefix:
            raise ValueError("Unexpected Address Prefix")
        puzzle_hash: bytes32 = decode_puzzle_hash(address)

        memos: List[bytes] = []
        if "memos" in request:
            memos = [mem.encode("utf-8") for mem in request["memos"]]

        fee: uint64 = uint64(request.get("fee", 0))
        min_coin_amount: uint64 = uint64(request.get("min_coin_amount", 0))
        async with self.service.wallet_state_manager.lock:
            tx: TransactionRecord = await wallet.generate_signed_transaction(
                amount, puzzle_hash, fee, memos=memos, min_coin_amount=min_coin_amount
            )
            await wallet.push_transaction(tx)

        # Transaction may not have been included in the mempool yet. Use get_transaction to check.
        return {
            "transaction": tx.to_json_dict_convenience(self.service.config),
            "transaction_id": tx.name,
        }

    async def send_transaction_multi(self, request) -> EndpointResult:
        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced before sending transactions")

        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(wallet, Wallet)

        async with self.service.wallet_state_manager.lock:
            transaction: Dict = (await self.create_signed_transaction(request, hold_lock=False))["signed_tx"]
            tr: TransactionRecord = TransactionRecord.from_json_dict_convenience(transaction)
            await wallet.push_transaction(tr)

        # Transaction may not have been included in the mempool yet. Use get_transaction to check.
        return {"transaction": transaction, "transaction_id": tr.name}

    async def delete_unconfirmed_transactions(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        if wallet_id not in self.service.wallet_state_manager.wallets:
            raise ValueError(f"Wallet id {wallet_id} does not exist")
        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced.")

        async with self.service.wallet_state_manager.db_wrapper.writer():
            await self.service.wallet_state_manager.tx_store.delete_unconfirmed_transactions(wallet_id)
            wallet = self.service.wallet_state_manager.wallets[wallet_id]
            if wallet.type() == WalletType.POOLING_WALLET.value:
                assert isinstance(wallet, PoolWallet)
                wallet.target_state = None
            return {}

    async def select_coins(self, request) -> EndpointResult:
        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced before selecting coins")

        amount = uint64(request["amount"])
        wallet_id = uint32(request["wallet_id"])
        min_coin_amount = uint64(request.get("min_coin_amount", 0))
        excluded_coins: Optional[List] = request.get("excluded_coins")
        if excluded_coins is not None:
            excluded_coins = [Coin.from_json_dict(json_coin) for json_coin in excluded_coins]

        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        async with self.service.wallet_state_manager.lock:
            selected_coins = await wallet.select_coins(
                amount=amount, min_coin_amount=min_coin_amount, exclude=excluded_coins
            )

        return {"coins": [coin.to_json_dict() for coin in selected_coins]}

    async def get_current_derivation_index(self, request) -> Dict[str, Any]:
        assert self.service.wallet_state_manager is not None

        index: Optional[uint32] = await self.service.wallet_state_manager.puzzle_store.get_last_derivation_path()

        return {"success": True, "index": index}

    async def extend_derivation_index(self, request) -> Dict[str, Any]:
        assert self.service.wallet_state_manager is not None

        # Require a new max derivation index
        if "index" not in request:
            raise ValueError("Derivation index is required")

        # Require that the wallet is fully synced
        synced = await self.service.wallet_state_manager.synced()
        if synced is False:
            raise ValueError("Wallet needs to be fully synced before extending derivation index")

        index = uint32(request["index"])
        current: Optional[uint32] = await self.service.wallet_state_manager.puzzle_store.get_last_derivation_path()

        # Additional sanity check that the wallet is synced
        if current is None:
            raise ValueError("No current derivation record found, unable to extend index")

        # Require that the new index is greater than the current index
        if index <= current:
            raise ValueError(f"New derivation index must be greater than current index: {current}")

        if index - current > MAX_DERIVATION_INDEX_DELTA:
            raise ValueError(
                "Too many derivations requested. "
                f"Use a derivation index less than {current + MAX_DERIVATION_INDEX_DELTA + 1}"
            )

        # Since we've bumping the derivation index without having found any new puzzles, we want
        # to preserve the current last used index, so we call create_more_puzzle_hashes with
        # mark_existing_as_used=False
        await self.service.wallet_state_manager.create_more_puzzle_hashes(
            from_zero=False, mark_existing_as_used=False, up_to_index=index, num_additional_phs=0
        )

        updated: Optional[uint32] = await self.service.wallet_state_manager.puzzle_store.get_last_derivation_path()
        updated_index = updated if updated is not None else None

        return {"success": True, "index": updated_index}

    async def get_notifications(self, request) -> EndpointResult:
        ids: Optional[List[str]] = request.get("ids", None)
        start: Optional[int] = request.get("start", None)
        end: Optional[int] = request.get("end", None)
        if ids is None:
            notifications: List[
                Notification
            ] = await self.service.wallet_state_manager.notification_manager.notification_store.get_all_notifications(
                pagination=(start, end)
            )
        else:
            notifications = (
                await self.service.wallet_state_manager.notification_manager.notification_store.get_notifications(
                    [bytes32.from_hexstr(id) for id in ids]
                )
            )

        return {
            "notifications": [
                {"id": notification.coin_id.hex(), "message": notification.message.hex(), "amount": notification.amount}
                for notification in notifications
            ]
        }

    async def delete_notifications(self, request) -> EndpointResult:
        ids: Optional[List[str]] = request.get("ids", None)
        if ids is None:
            await self.service.wallet_state_manager.notification_manager.notification_store.delete_all_notifications()
        else:
            await self.service.wallet_state_manager.notification_manager.notification_store.delete_notifications(
                [bytes32.from_hexstr(id) for id in ids]
            )

        return {}

    async def send_notification(self, request) -> EndpointResult:
        tx: TransactionRecord = await self.service.wallet_state_manager.notification_manager.send_new_notification(
            bytes32.from_hexstr(request["target"]),
            bytes.fromhex(request["message"]),
            uint64(request["amount"]),
            request.get("fee", uint64(0)),
        )
        await self.service.wallet_state_manager.add_pending_transaction(tx)
        return {"tx": tx.to_json_dict_convenience(self.service.config)}

    async def sign_message_by_address(self, request) -> EndpointResult:
        """
        Given a derived P2 address, sign the message by its private key.
        :param request:
        :return:
        """
        puzzle_hash: bytes32 = decode_puzzle_hash(request["address"])
        pubkey, signature = await self.service.wallet_state_manager.main_wallet.sign_message(
            request["message"], puzzle_hash
        )
        return {"success": True, "pubkey": str(pubkey), "signature": str(signature)}

    async def sign_message_by_id(self, request) -> EndpointResult:
        """
        Given a NFT/DID ID, sign the message by the P2 private key.
        :param request:
        :return:
        """

        entity_id: bytes32 = decode_puzzle_hash(request["id"])
        selected_wallet: Optional[WalletProtocol] = None
        if is_valid_address(request["id"], {AddressType.DID}, self.service.config):
            for wallet in self.service.wallet_state_manager.wallets.values():
                if wallet.type() == WalletType.DECENTRALIZED_ID.value:
                    assert isinstance(wallet, DIDWallet)
                    assert wallet.did_info.origin_coin is not None
                    if wallet.did_info.origin_coin.name() == entity_id:
                        selected_wallet = wallet
                        break
            if selected_wallet is None:
                return {"success": False, "error": f"DID for {entity_id.hex()} doesn't exist."}
            assert isinstance(selected_wallet, DIDWallet)
            pubkey, signature = await selected_wallet.sign_message(request["message"])
        elif is_valid_address(request["id"], {AddressType.NFT}, self.service.config):
            target_nft: Optional[NFTCoinInfo] = None
            for wallet in self.service.wallet_state_manager.wallets.values():
                if wallet.type() == WalletType.NFT.value:
                    assert isinstance(wallet, NFTWallet)
                    nft: Optional[NFTCoinInfo] = await wallet.get_nft(entity_id)
                    if nft is not None:
                        selected_wallet = wallet
                        target_nft = nft
                        break
            if selected_wallet is None or target_nft is None:
                return {"success": False, "error": f"NFT for {entity_id.hex()} doesn't exist."}

            assert isinstance(selected_wallet, NFTWallet)
            pubkey, signature = await selected_wallet.sign_message(request["message"], target_nft)
        else:
            return {"success": False, "error": f'Unknown ID type, {request["id"]}'}

        return {"success": True, "pubkey": str(pubkey), "signature": str(signature)}

    ##########################################################################################
    # CATs and Trading
    ##########################################################################################

    async def get_cat_list(self, request) -> EndpointResult:
        return {"cat_list": list(DEFAULT_CATS.values())}

    async def cat_set_name(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(wallet, CATWallet)
        await wallet.set_name(str(request["name"]))
        return {"wallet_id": wallet_id}

    async def cat_get_name(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(wallet, CATWallet)
        name: str = await wallet.get_name()
        return {"wallet_id": wallet_id, "name": name}

    async def get_stray_cats(self, request) -> EndpointResult:
        """
        Get a list of all unacknowledged CATs
        :param request: RPC request
        :return: A list of unacknowledged CATs
        """
        cats = await self.service.wallet_state_manager.interested_store.get_unacknowledged_tokens()
        return {"stray_cats": cats}

    async def cat_spend(self, request) -> EndpointResult:
        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced.")
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(wallet, CATWallet)

        puzzle_hash: bytes32 = decode_puzzle_hash(request["inner_address"])

        memos: List[bytes] = []
        if "memos" in request:
            memos = [mem.encode("utf-8") for mem in request["memos"]]
        if not isinstance(request["amount"], int) or not isinstance(request["fee"], int):
            raise ValueError("An integer amount or fee is required (too many decimals)")
        amount: uint64 = uint64(request["amount"])
        fee: uint64 = uint64(request.get("fee", 0))
        min_coin_amount: uint64 = uint64(request.get("min_coin_amount", 0))
        async with self.service.wallet_state_manager.lock:
            txs: List[TransactionRecord] = await wallet.generate_signed_transaction(
                [amount], [puzzle_hash], fee, memos=[memos], min_coin_amount=min_coin_amount
            )
            for tx in txs:
                await wallet.standard_wallet.push_transaction(tx)

        return {
            "transaction": tx.to_json_dict_convenience(self.service.config),
            "transaction_id": tx.name,
        }

    async def cat_get_asset_id(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(wallet, CATWallet)
        asset_id: str = wallet.get_asset_id()
        return {"asset_id": asset_id, "wallet_id": wallet_id}

    async def cat_asset_id_to_name(self, request) -> EndpointResult:
        wallet = await self.service.wallet_state_manager.get_wallet_for_asset_id(request["asset_id"])
        if wallet is None:
            if request["asset_id"] in DEFAULT_CATS:
                return {"wallet_id": None, "name": DEFAULT_CATS[request["asset_id"]]["name"]}
            else:
                raise ValueError("The asset ID specified does not belong to a wallet")
        else:
            return {"wallet_id": wallet.id(), "name": (await wallet.get_name())}

    async def create_offer_for_ids(self, request) -> EndpointResult:
        offer: Dict[str, int] = request["offer"]
        fee: uint64 = uint64(request.get("fee", 0))
        validate_only: bool = request.get("validate_only", False)
        driver_dict_str: Optional[Dict[str, Any]] = request.get("driver_dict", None)
        min_coin_amount: uint64 = uint64(request.get("min_coin_amount", 0))
        marshalled_solver = request.get("solver")
        solver: Optional[Solver]
        if marshalled_solver is None:
            solver = None
        else:
            solver = Solver(info=marshalled_solver)

        # This driver_dict construction is to maintain backward compatibility where everything is assumed to be a CAT
        driver_dict: Dict[bytes32, PuzzleInfo] = {}
        if driver_dict_str is None:
            for key, amount in offer.items():
                if amount > 0:
                    try:
                        driver_dict[bytes32.from_hexstr(key)] = PuzzleInfo(
                            {"type": AssetType.CAT.value, "tail": "0x" + key}
                        )
                    except ValueError:
                        pass
        else:
            for key, value in driver_dict_str.items():
                driver_dict[bytes32.from_hexstr(key)] = PuzzleInfo(value)

        modified_offer: Dict[Union[int, bytes32], int] = {}
        for key in offer:
            try:
                modified_offer[bytes32.from_hexstr(key)] = offer[key]
            except ValueError:
                modified_offer[int(key)] = offer[key]

        async with self.service.wallet_state_manager.lock:
            result = await self.service.wallet_state_manager.trade_manager.create_offer_for_ids(
                modified_offer,
                driver_dict,
                solver=solver,
                fee=fee,
                validate_only=validate_only,
                min_coin_amount=min_coin_amount,
            )
        if result[0]:
            success, trade_record, error = result
            return {
                "offer": Offer.from_bytes(trade_record.offer).to_bech32(),
                "trade_record": trade_record.to_json_dict_convenience(),
            }
        raise ValueError(result[2])

    async def get_offer_summary(self, request) -> EndpointResult:
        offer_hex: str = request["offer"]
        offer = Offer.from_bech32(offer_hex)
        offered, requested, infos = offer.summary()

        ###
        # This is temporary code, delete it when we no longer care about incorrectly parsing CAT1s
        # There's also temp code in test_wallet_rpc.py and wallet_funcs.py
        from chia.util.bech32m import bech32_decode, convertbits
        from chia.wallet.util.puzzle_compression import decompress_object_with_puzzles

        hrpgot, data = bech32_decode(offer_hex, max_length=len(offer_hex))
        if data is None:
            raise ValueError("Invalid Offer")
        decoded = convertbits(list(data), 5, 8, False)
        decoded_bytes = bytes(decoded)
        try:
            decompressed_bytes = decompress_object_with_puzzles(decoded_bytes)
        except TypeError:
            decompressed_bytes = decoded_bytes
        bundle = SpendBundle.from_bytes(decompressed_bytes)
        for spend in bundle.coin_spends:
            mod, _ = spend.puzzle_reveal.to_program().uncurry()
            if mod.get_tree_hash() == bytes32.from_hexstr(
                "72dec062874cd4d3aab892a0906688a1ae412b0109982e1797a170add88bdcdc"
            ):
                raise ValueError("CAT1s are no longer supported")
        ###

        if request.get("advanced", False):
            return {
                "summary": {"offered": offered, "requested": requested, "fees": offer.bundle.fees(), "infos": infos}
            }
        else:
            return {"summary": await self.service.wallet_state_manager.trade_manager.get_offer_summary(offer)}

    async def check_offer_validity(self, request) -> EndpointResult:
        offer_hex: str = request["offer"]
        offer = Offer.from_bech32(offer_hex)
        peer: Optional[WSChiaConnection] = self.service.get_full_node_peer()
        if peer is None:
            raise ValueError("No peer connected")
        return {"valid": (await self.service.wallet_state_manager.trade_manager.check_offer_validity(offer, peer))}

    async def take_offer(self, request) -> EndpointResult:
        offer_hex: str = request["offer"]
        offer = Offer.from_bech32(offer_hex)
        fee: uint64 = uint64(request.get("fee", 0))
        min_coin_amount: uint64 = uint64(request.get("min_coin_amount", 0))
        maybe_marshalled_solver: Dict[str, Any] = request.get("solver")
        solver: Optional[Solver]
        if maybe_marshalled_solver is None:
            solver = None
        else:
            solver = Solver(info=maybe_marshalled_solver)

        async with self.service.wallet_state_manager.lock:
            peer: Optional[WSChiaConnection] = self.service.get_full_node_peer()
            if peer is None:
                raise ValueError("No peer connected")
            result = await self.service.wallet_state_manager.trade_manager.respond_to_offer(
                offer, peer, fee=fee, min_coin_amount=min_coin_amount, solver=solver
            )
        if not result[0]:
            raise ValueError(result[2])
        success, trade_record, error = result
        return {"trade_record": trade_record.to_json_dict_convenience()}

    async def get_offer(self, request: Dict) -> EndpointResult:
        trade_mgr = self.service.wallet_state_manager.trade_manager

        trade_id = bytes32.from_hexstr(request["trade_id"])
        file_contents: bool = request.get("file_contents", False)
        trade_record: Optional[TradeRecord] = await trade_mgr.get_trade_by_id(bytes32(trade_id))
        if trade_record is None:
            raise ValueError(f"No trade with trade id: {trade_id.hex()}")

        offer_to_return: bytes = trade_record.offer if trade_record.taken_offer is None else trade_record.taken_offer
        offer_value: Optional[str] = Offer.from_bytes(offer_to_return).to_bech32() if file_contents else None
        return {"trade_record": trade_record.to_json_dict_convenience(), "offer": offer_value}

    async def get_all_offers(self, request: Dict) -> EndpointResult:
        trade_mgr = self.service.wallet_state_manager.trade_manager

        start: int = request.get("start", 0)
        end: int = request.get("end", 10)
        exclude_my_offers: bool = request.get("exclude_my_offers", False)
        exclude_taken_offers: bool = request.get("exclude_taken_offers", False)
        include_completed: bool = request.get("include_completed", False)
        sort_key: Optional[str] = request.get("sort_key", None)
        reverse: bool = request.get("reverse", False)
        file_contents: bool = request.get("file_contents", False)

        all_trades = await trade_mgr.trade_store.get_trades_between(
            start,
            end,
            sort_key=sort_key,
            reverse=reverse,
            exclude_my_offers=exclude_my_offers,
            exclude_taken_offers=exclude_taken_offers,
            include_completed=include_completed,
        )
        result = []
        offer_values: Optional[List[str]] = [] if file_contents else None
        for trade in all_trades:
            result.append(trade.to_json_dict_convenience())
            if file_contents and offer_values is not None:
                offer_to_return: bytes = trade.offer if trade.taken_offer is None else trade.taken_offer
                offer_values.append(Offer.from_bytes(offer_to_return).to_bech32())

        return {"trade_records": result, "offers": offer_values}

    async def get_offers_count(self, request: Dict) -> EndpointResult:
        trade_mgr = self.service.wallet_state_manager.trade_manager

        (total, my_offers_count, taken_offers_count) = await trade_mgr.trade_store.get_trades_count()

        return {"total": total, "my_offers_count": my_offers_count, "taken_offers_count": taken_offers_count}

    async def cancel_offer(self, request: Dict) -> EndpointResult:
        wsm = self.service.wallet_state_manager
        secure = request["secure"]
        trade_id = bytes32.from_hexstr(request["trade_id"])
        fee: uint64 = uint64(request.get("fee", 0))
        async with self.service.wallet_state_manager.lock:
            if secure:
                await wsm.trade_manager.cancel_pending_offer_safely(bytes32(trade_id), fee=fee)
            else:
                await wsm.trade_manager.cancel_pending_offer(bytes32(trade_id))
        return {}

    async def cancel_offers(self, request: Dict) -> EndpointResult:
        secure = request["secure"]
        batch_fee: uint64 = uint64(request.get("batch_fee", 0))
        batch_size = request.get("batch_size", 5)
        cancel_all = request.get("cancel_all", False)
        if cancel_all:
            asset_id = None
        else:
            asset_id = request.get("asset_id", "xch")

        start: int = 0
        end: int = start + batch_size
        trade_mgr = self.service.wallet_state_manager.trade_manager
        log.info(f"Start cancelling offers for  {'asset_id: ' + asset_id if asset_id is not None else 'all'} ...")
        # Traverse offers page by page
        key = None
        if asset_id is not None and asset_id != "xch":
            key = bytes32.from_hexstr(asset_id)
        while True:
            records: List[TradeRecord] = []
            trades = await trade_mgr.trade_store.get_trades_between(
                start,
                end,
                reverse=True,
                exclude_my_offers=False,
                exclude_taken_offers=True,
                include_completed=False,
            )
            for trade in trades:
                if cancel_all:
                    records.append(trade)
                    continue
                if trade.offer and trade.offer != b"":
                    offer = Offer.from_bytes(trade.offer)
                    if key in offer.driver_dict:
                        records.append(trade)
                        continue

            async with self.service.wallet_state_manager.lock:
                await trade_mgr.cancel_pending_offers(records, batch_fee, secure)
            log.info(f"Cancelled offers {start} to {end} ...")
            # If fewer records were returned than requested, we're done
            if len(trades) < batch_size:
                break
            start = end
            end += batch_size
        return {"success": True}

    ##########################################################################################
    # Distributed Identities
    ##########################################################################################

    async def did_set_wallet_name(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        if wallet.type() == WalletType.DECENTRALIZED_ID:
            assert isinstance(wallet, DIDWallet)
            await wallet.set_name(str(request["name"]))
            return {"success": True, "wallet_id": wallet_id}
        else:
            return {"success": False, "error": f"Wallet id {wallet_id} is not a DID wallet"}

    async def did_get_wallet_name(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(wallet, DIDWallet)
        name: str = await wallet.get_name()
        return {"success": True, "wallet_id": wallet_id, "name": name}

    async def did_update_recovery_ids(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(wallet, DIDWallet)
        recovery_list = []
        success: bool = False
        for _ in request["new_list"]:
            recovery_list.append(decode_puzzle_hash(_))
        if "num_verifications_required" in request:
            new_amount_verifications_required = uint64(request["num_verifications_required"])
        else:
            new_amount_verifications_required = uint64(len(recovery_list))
        async with self.service.wallet_state_manager.lock:
            update_success = await wallet.update_recovery_list(recovery_list, new_amount_verifications_required)
            # Update coin with new ID info
            if update_success:
                spend_bundle = await wallet.create_update_spend()
                if spend_bundle is not None:
                    success = True
        return {"success": success}

    async def did_update_metadata(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        if wallet.type() != WalletType.DECENTRALIZED_ID.value:
            return {"success": False, "error": f"Wallet with id {wallet_id} is not a DID one"}
        assert isinstance(wallet, DIDWallet)
        metadata: Dict[str, str] = {}
        if "metadata" in request and type(request["metadata"]) is dict:
            metadata = request["metadata"]
        async with self.service.wallet_state_manager.lock:
            update_success = await wallet.update_metadata(metadata)
            # Update coin with new ID info
            if update_success:
                spend_bundle = await wallet.create_update_spend(uint64(request.get("fee", 0)))
                if spend_bundle is not None:
                    return {"wallet_id": wallet_id, "success": True, "spend_bundle": spend_bundle}
                else:
                    return {"success": False, "error": "Couldn't create an update spend bundle."}
            else:
                return {"success": False, "error": f"Couldn't update metadata with input: {metadata}"}

    async def did_get_did(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(wallet, DIDWallet)
        my_did: str = encode_puzzle_hash(bytes32.fromhex(wallet.get_my_DID()), AddressType.DID.hrp(self.service.config))
        async with self.service.wallet_state_manager.lock:
            try:
                coins = await wallet.select_coins(uint64(1))
                coin = coins.pop()
                return {"success": True, "wallet_id": wallet_id, "my_did": my_did, "coin_id": coin.name()}
            except ValueError:
                return {"success": True, "wallet_id": wallet_id, "my_did": my_did}

    async def did_get_recovery_list(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(wallet, DIDWallet)
        recovery_list = wallet.did_info.backup_ids
        recovery_dids = []
        for backup_id in recovery_list:
            recovery_dids.append(encode_puzzle_hash(backup_id, AddressType.DID.hrp(self.service.config)))
        return {
            "success": True,
            "wallet_id": wallet_id,
            "recovery_list": recovery_dids,
            "num_required": wallet.did_info.num_of_backup_ids_needed,
        }

    async def did_get_metadata(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(wallet, DIDWallet)
        metadata = json.loads(wallet.did_info.metadata)
        return {
            "success": True,
            "wallet_id": wallet_id,
            "metadata": metadata,
        }

    async def did_recovery_spend(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(wallet, DIDWallet)
        if len(request["attest_data"]) < wallet.did_info.num_of_backup_ids_needed:
            return {"success": False, "reason": "insufficient messages"}
        spend_bundle = None
        async with self.service.wallet_state_manager.lock:
            (
                info_list,
                message_spend_bundle,
            ) = await wallet.load_attest_files_for_recovery_spend(request["attest_data"])

            if "pubkey" in request:
                pubkey = G1Element.from_bytes(hexstr_to_bytes(request["pubkey"]))
            else:
                assert wallet.did_info.temp_pubkey is not None
                pubkey = wallet.did_info.temp_pubkey

            if "puzhash" in request:
                puzhash = bytes32.from_hexstr(request["puzhash"])
            else:
                assert wallet.did_info.temp_puzhash is not None
                puzhash = wallet.did_info.temp_puzhash

            assert wallet.did_info.temp_coin is not None
            spend_bundle = await wallet.recovery_spend(
                wallet.did_info.temp_coin,
                puzhash,
                info_list,
                pubkey,
                message_spend_bundle,
            )
        if spend_bundle:
            return {"success": True, "spend_bundle": spend_bundle}
        else:
            return {"success": False}

    async def did_get_pubkey(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(wallet, DIDWallet)
        pubkey = bytes((await wallet.wallet_state_manager.get_unused_derivation_record(wallet_id)).pubkey).hex()
        return {"success": True, "pubkey": pubkey}

    async def did_create_attest(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(wallet, DIDWallet)
        async with self.service.wallet_state_manager.lock:
            info = await wallet.get_info_for_recovery()
            coin = bytes32.from_hexstr(request["coin_name"])
            pubkey = G1Element.from_bytes(hexstr_to_bytes(request["pubkey"]))
            spend_bundle, attest_data = await wallet.create_attestment(
                coin,
                bytes32.from_hexstr(request["puzhash"]),
                pubkey,
            )
        if info is not None and spend_bundle is not None:
            return {
                "success": True,
                "message_spend_bundle": bytes(spend_bundle).hex(),
                "info": [info[0].hex(), info[1].hex(), info[2]],
                "attest_data": attest_data,
            }
        else:
            return {"success": False}

    async def did_get_information_needed_for_recovery(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        did_wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(did_wallet, DIDWallet)
        my_did = encode_puzzle_hash(
            bytes32.from_hexstr(did_wallet.get_my_DID()), AddressType.DID.hrp(self.service.config)
        )
        assert did_wallet.did_info.temp_coin is not None
        coin_name = did_wallet.did_info.temp_coin.name().hex()
        return {
            "success": True,
            "wallet_id": wallet_id,
            "my_did": my_did,
            "coin_name": coin_name,
            "newpuzhash": did_wallet.did_info.temp_puzhash,
            "pubkey": did_wallet.did_info.temp_pubkey,
            "backup_dids": did_wallet.did_info.backup_ids,
        }

    async def did_get_current_coin_info(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        did_wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(did_wallet, DIDWallet)
        my_did = encode_puzzle_hash(
            bytes32.from_hexstr(did_wallet.get_my_DID()), AddressType.DID.hrp(self.service.config)
        )
        did_coin_threeple = await did_wallet.get_info_for_recovery()
        assert my_did is not None
        assert did_coin_threeple is not None
        return {
            "success": True,
            "wallet_id": wallet_id,
            "my_did": my_did,
            "did_parent": did_coin_threeple[0],
            "did_innerpuz": did_coin_threeple[1],
            "did_amount": did_coin_threeple[2],
        }

    async def did_create_backup_file(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        did_wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(did_wallet, DIDWallet)
        return {"wallet_id": wallet_id, "success": True, "backup_data": did_wallet.create_backup()}

    async def did_transfer_did(self, request) -> EndpointResult:
        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced.")
        wallet_id = uint32(request["wallet_id"])
        did_wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(did_wallet, DIDWallet)
        puzzle_hash: bytes32 = decode_puzzle_hash(request["inner_address"])
        async with self.service.wallet_state_manager.lock:
            txs: TransactionRecord = await did_wallet.transfer_did(
                puzzle_hash, uint64(request.get("fee", 0)), request.get("with_recovery_info", True)
            )

        return {
            "success": True,
            "transaction": txs.to_json_dict_convenience(self.service.config),
            "transaction_id": txs.name,
        }

    ##########################################################################################
    # NFT Wallet
    ##########################################################################################

    async def nft_mint_nft(self, request) -> EndpointResult:
        log.debug("Got minting RPC request: %s", request)
        wallet_id = uint32(request["wallet_id"])
        assert self.service.wallet_state_manager
        nft_wallet = self.service.wallet_state_manager.wallets[wallet_id]
        if nft_wallet.type() != WalletType.NFT.value:
            return {"success": False, "error": f"Wallet with id {wallet_id} is not an NFT one"}
        assert isinstance(nft_wallet, NFTWallet)
        royalty_address = request.get("royalty_address")
        royalty_amount = uint16(request.get("royalty_percentage", 0))
        if royalty_amount == 10000:
            raise ValueError("Royalty percentage cannot be 100%")
        if isinstance(royalty_address, str):
            royalty_puzhash = decode_puzzle_hash(royalty_address)
        elif royalty_address is None:
            royalty_puzhash = await nft_wallet.standard_wallet.get_new_puzzlehash()
        else:
            royalty_puzhash = royalty_address
        target_address = request.get("target_address")
        if isinstance(target_address, str):
            target_puzhash = decode_puzzle_hash(target_address)
        elif target_address is None:
            target_puzhash = await nft_wallet.standard_wallet.get_new_puzzlehash()
        else:
            target_puzhash = target_address
        if "uris" not in request:
            return {"success": False, "error": "Data URIs is required"}
        if not isinstance(request["uris"], list):
            return {"success": False, "error": "Data URIs must be a list"}
        if not isinstance(request.get("meta_uris", []), list):
            return {"success": False, "error": "Metadata URIs must be a list"}
        if not isinstance(request.get("license_uris", []), list):
            return {"success": False, "error": "License URIs must be a list"}
        metadata_list = [
            ("u", request["uris"]),
            ("h", hexstr_to_bytes(request["hash"])),
            ("mu", request.get("meta_uris", [])),
            ("lu", request.get("license_uris", [])),
            ("sn", uint64(request.get("edition_number", 1))),
            ("st", uint64(request.get("edition_total", 1))),
        ]
        if "meta_hash" in request and len(request["meta_hash"]) > 0:
            metadata_list.append(("mh", hexstr_to_bytes(request["meta_hash"])))
        if "license_hash" in request and len(request["license_hash"]) > 0:
            metadata_list.append(("lh", hexstr_to_bytes(request["license_hash"])))
        metadata = Program.to(metadata_list)
        fee = uint64(request.get("fee", 0))
        did_id = request.get("did_id", None)
        if did_id is not None:
            if did_id == "":
                did_id = bytes()
            else:
                did_id = decode_puzzle_hash(did_id)
        spend_bundle = await nft_wallet.generate_new_nft(
            metadata,
            target_puzhash,
            royalty_puzhash,
            royalty_amount,
            did_id,
            fee,
        )
        return {"wallet_id": wallet_id, "success": True, "spend_bundle": spend_bundle}

    async def nft_get_nfts(self, request) -> EndpointResult:
        wallet_id = request.get("wallet_id", None)
        nfts: List[NFTCoinInfo] = []
        if wallet_id is not None:
            nft_wallet: WalletProtocol = self.service.wallet_state_manager.wallets[wallet_id]
            assert isinstance(nft_wallet, NFTWallet)
            nfts = await nft_wallet.get_current_nfts()
        else:
            for wallet in self.service.wallet_state_manager.wallets.values():
                if wallet.type() == WalletType.NFT.value:
                    assert isinstance(wallet, NFTWallet)
                    nfts.extend(await wallet.get_current_nfts())
        start_index = request.get("start_index", 0)
        num = request.get("num", len(nfts))
        nft_info_list = []
        count = 0
        for nft in nfts:
            if count >= start_index and count - start_index < num:
                nft_info = await nft_puzzles.get_nft_info_from_puzzle(
                    nft,
                    self.service.wallet_state_manager.config,
                    request.get("include_off_chain_metadata", False),
                    request.get("ignore_size_limit", False),
                )
                nft_info_list.append(nft_info)
            count += 1
        return {"wallet_id": wallet_id, "success": True, "nft_list": nft_info_list}

    async def nft_set_nft_did(self, request):
        wallet_id = uint32(request["wallet_id"])
        nft_wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(nft_wallet, NFTWallet)
        did_id = request.get("did_id", "")
        if did_id == "":
            did_id = b""
        else:
            did_id = decode_puzzle_hash(did_id)
        nft_coin_info = await nft_wallet.get_nft_coin_by_id(bytes32.from_hexstr(request["nft_coin_id"]))
        if not (
            await nft_puzzles.get_nft_info_from_puzzle(nft_coin_info, self.service.wallet_state_manager.config)
        ).supports_did:
            return {"success": False, "error": "The NFT doesn't support setting a DID."}
        fee = uint64(request.get("fee", 0))
        spend_bundle = await nft_wallet.set_nft_did(nft_coin_info, did_id, fee=fee)
        return {"wallet_id": wallet_id, "success": True, "spend_bundle": spend_bundle}

    async def nft_get_by_did(self, request) -> EndpointResult:
        did_id: Optional[bytes32] = None
        if "did_id" in request:
            did_id = decode_puzzle_hash(request["did_id"])
        for wallet in self.service.wallet_state_manager.wallets.values():
            if isinstance(wallet, NFTWallet) and wallet.get_did() == did_id:
                return {"wallet_id": wallet.wallet_id, "success": True}
        return {"error": f"Cannot find a NFT wallet DID = {did_id}", "success": False}

    async def nft_get_wallet_did(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        nft_wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(nft_wallet, NFTWallet)
        if nft_wallet is not None:
            if nft_wallet.type() != WalletType.NFT.value:
                return {"success": False, "error": f"Wallet {wallet_id} is not an NFT wallet"}
            did_bytes: Optional[bytes32] = nft_wallet.get_did()
            did_id = ""
            if did_bytes is not None:
                did_id = encode_puzzle_hash(did_bytes, AddressType.DID.hrp(self.service.config))
            return {"success": True, "did_id": None if len(did_id) == 0 else did_id}
        return {"success": False, "error": f"Wallet {wallet_id} not found"}

    async def nft_get_wallets_with_dids(self, request) -> EndpointResult:
        all_wallets = self.service.wallet_state_manager.wallets.values()
        did_wallets_by_did_id: Dict[bytes32, uint32] = {}

        for wallet in all_wallets:
            if wallet.type() == uint8(WalletType.DECENTRALIZED_ID):
                assert isinstance(wallet, DIDWallet)
                if wallet.did_info.origin_coin is not None:
                    did_wallets_by_did_id[wallet.did_info.origin_coin.name()] = wallet.id()

        did_nft_wallets: List[Dict] = []
        for wallet in all_wallets:
            if isinstance(wallet, NFTWallet):
                nft_wallet_did: Optional[bytes32] = wallet.get_did()
                if nft_wallet_did is not None:
                    did_wallet_id: uint32 = did_wallets_by_did_id.get(nft_wallet_did, uint32(0))
                    if did_wallet_id == 0:
                        log.warning(f"NFT wallet {wallet.id()} has DID {nft_wallet_did.hex()} but no DID wallet")
                    else:
                        did_nft_wallets.append(
                            {
                                "wallet_id": wallet.id(),
                                "did_id": encode_puzzle_hash(nft_wallet_did, AddressType.DID.hrp(self.service.config)),
                                "did_wallet_id": did_wallet_id,
                            }
                        )
        return {"success": True, "nft_wallets": did_nft_wallets}

    async def nft_set_nft_status(self, request) -> EndpointResult:
        wallet_id: uint32 = uint32(request["wallet_id"])
        coin_id: bytes32 = bytes32.from_hexstr(request["coin_id"])
        status: bool = request["in_transaction"]
        assert self.service.wallet_state_manager is not None
        nft_wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(nft_wallet, NFTWallet)
        if nft_wallet is not None:
            await nft_wallet.update_coin_status(coin_id, status)
            return {"success": True}
        return {"success": False, "error": "NFT wallet doesn't exist."}

    async def nft_transfer_nft(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        address = request["target_address"]
        if isinstance(address, str):
            puzzle_hash = decode_puzzle_hash(address)
        else:
            return dict(success=False, error="target_address parameter missing")
        nft_wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(nft_wallet, NFTWallet)
        try:
            nft_coin_id = request["nft_coin_id"]
            if nft_coin_id.startswith(AddressType.NFT.hrp(self.service.config)):
                nft_coin_id = decode_puzzle_hash(nft_coin_id)
            else:
                nft_coin_id = bytes32.from_hexstr(nft_coin_id)
            nft_coin_info = await nft_wallet.get_nft_coin_by_id(nft_coin_id)
            fee = uint64(request.get("fee", 0))
            txs = await nft_wallet.generate_signed_transaction(
                [uint64(nft_coin_info.coin.amount)],
                [puzzle_hash],
                coins={nft_coin_info.coin},
                fee=fee,
                new_owner=b"",
                new_did_inner_hash=b"",
            )
            spend_bundle: Optional[SpendBundle] = None
            for tx in txs:
                if tx.spend_bundle is not None:
                    spend_bundle = tx.spend_bundle
                await self.service.wallet_state_manager.add_pending_transaction(tx)
            await nft_wallet.update_coin_status(nft_coin_info.coin.name(), True)
            return {"wallet_id": wallet_id, "success": True, "spend_bundle": spend_bundle}
        except Exception as e:
            log.exception(f"Failed to transfer NFT: {e}")
            return {"success": False, "error": str(e)}

    async def nft_get_info(self, request: Dict) -> EndpointResult:
        if "coin_id" not in request:
            return {"success": False, "error": "Coin ID is required."}
        coin_id = request["coin_id"]
        if coin_id.startswith(AddressType.NFT.hrp(self.service.config)):
            coin_id = decode_puzzle_hash(coin_id)
        else:
            coin_id = bytes32.from_hexstr(coin_id)
        # Get coin state
        peer: Optional[WSChiaConnection] = self.service.get_full_node_peer()
        if peer is None:
            raise ValueError("No peers to get info from")
        coin_state_list: List[CoinState] = await self.service.wallet_state_manager.wallet_node.get_coin_state(
            [coin_id], peer=peer
        )
        if coin_state_list is None or len(coin_state_list) < 1:
            return {"success": False, "error": f"Coin record 0x{coin_id.hex()} not found"}
        coin_state: CoinState = coin_state_list[0]
        if request.get("latest", True):
            # Find the unspent coin
            while coin_state.spent_height is not None:
                coin_state_list = await self.service.wallet_state_manager.wallet_node.fetch_children(
                    coin_state.coin.name(), peer=peer
                )
                odd_coin = 0
                for coin in coin_state_list:
                    if coin.coin.amount % 2 == 1:
                        odd_coin += 1
                    if odd_coin > 1:
                        return {"success": False, "error": "This is not a singleton, multiple children coins found."}
                if odd_coin == 0:
                    return {"success": False, "error": "Cannot find child coin, please wait then retry."}
                coin_state = coin_state_list[0]
        # Get parent coin
        parent_coin_state_list: List[CoinState] = await self.service.wallet_state_manager.wallet_node.get_coin_state(
            [coin_state.coin.parent_coin_info], peer=peer
        )
        if parent_coin_state_list is None or len(parent_coin_state_list) < 1:
            return {
                "success": False,
                "error": f"Parent coin record 0x{coin_state.coin.parent_coin_info.hex()} not found",
            }
        parent_coin_state: CoinState = parent_coin_state_list[0]
        coin_spend: CoinSpend = await self.service.wallet_state_manager.wallet_node.fetch_puzzle_solution(
            parent_coin_state.spent_height, parent_coin_state.coin, peer
        )
        # convert to NFTInfo
        # Check if the metadata is updated
        full_puzzle: Program = Program.from_bytes(bytes(coin_spend.puzzle_reveal))

        uncurried_nft: Optional[UncurriedNFT] = UncurriedNFT.uncurry(*full_puzzle.uncurry())
        if uncurried_nft is None:
            return {"success": False, "error": "The coin is not a NFT."}
        metadata, p2_puzzle_hash = get_metadata_and_phs(uncurried_nft, coin_spend.solution)
        # Note: This is not the actual unspent NFT full puzzle.
        # There is no way to rebuild the full puzzle in a different wallet.
        # But it shouldn't have impact on generating the NFTInfo, since inner_puzzle is not used there.
        if uncurried_nft.supports_did:
            inner_puzzle = nft_puzzles.recurry_nft_puzzle(
                uncurried_nft, coin_spend.solution.to_program(), uncurried_nft.p2_puzzle
            )
        else:
            inner_puzzle = uncurried_nft.p2_puzzle

        full_puzzle = nft_puzzles.create_full_puzzle(
            uncurried_nft.singleton_launcher_id,
            metadata,
            uncurried_nft.metadata_updater_hash,
            inner_puzzle,
        )

        # Get launcher coin
        launcher_coin: List[CoinState] = await self.service.wallet_state_manager.wallet_node.get_coin_state(
            [uncurried_nft.singleton_launcher_id], peer=peer
        )
        if launcher_coin is None or len(launcher_coin) < 1 or launcher_coin[0].spent_height is None:
            return {
                "success": False,
                "error": f"Launcher coin record 0x{uncurried_nft.singleton_launcher_id.hex()} not found",
            }
        minter_did = await self.service.wallet_state_manager.get_minter_did(launcher_coin[0].coin, peer)

        nft_info: NFTInfo = await nft_puzzles.get_nft_info_from_puzzle(
            NFTCoinInfo(
                uncurried_nft.singleton_launcher_id,
                coin_state.coin,
                None,
                full_puzzle,
                uint32(launcher_coin[0].spent_height),
                minter_did,
                uint32(coin_state.created_height) if coin_state.created_height else uint32(0),
            ),
            self.service.wallet_state_manager.config,
            request.get("include_off_chain_metadata", False),
            request.get("ignore_size_limit", False),
        )
        # This is a bit hacky, it should just come out like this, but this works for this RPC
        nft_info = dataclasses.replace(nft_info, p2_address=p2_puzzle_hash)
        return {"success": True, "nft_info": nft_info}

    async def nft_add_uri(self, request) -> EndpointResult:
        wallet_id = uint32(request["wallet_id"])
        # Note metadata updater can only add one uri for one field per spend.
        # If you want to add multiple uris for one field, you need to spend multiple times.
        nft_wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(nft_wallet, NFTWallet)
        uri = request["uri"]
        key = request["key"]
        nft_coin_id = request["nft_coin_id"]
        if nft_coin_id.startswith(AddressType.NFT.hrp(self.service.config)):
            nft_coin_id = decode_puzzle_hash(nft_coin_id)
        else:
            nft_coin_id = bytes32.from_hexstr(nft_coin_id)
        nft_coin_info = await nft_wallet.get_nft_coin_by_id(nft_coin_id)
        fee = uint64(request.get("fee", 0))
        spend_bundle = await nft_wallet.update_metadata(nft_coin_info, key, uri, fee=fee)
        return {"wallet_id": wallet_id, "success": True, "spend_bundle": spend_bundle}

    async def nft_calculate_royalties(self, request) -> EndpointResult:
        return NFTWallet.royalty_calculation(
            {
                asset["asset"]: (asset["royalty_address"], uint16(asset["royalty_percentage"]))
                for asset in request.get("royalty_assets", [])
            },
            {asset["asset"]: uint64(asset["amount"]) for asset in request.get("fungible_assets", [])},
        )

    async def nft_mint_bulk(self, request) -> EndpointResult:
        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced.")
        wallet_id = uint32(request["wallet_id"])
        nft_wallet: WalletProtocol = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(nft_wallet, NFTWallet)
        if nft_wallet.type() != WalletType.NFT.value:
            raise ValueError("The provided Wallet ID is not a NFT wallet")
        royalty_address = request.get("royalty_address", None)
        if isinstance(royalty_address, str) and royalty_address != "":
            royalty_puzhash = decode_puzzle_hash(royalty_address)
        elif royalty_address in [None, ""]:
            royalty_puzhash = await nft_wallet.standard_wallet.get_new_puzzlehash()
        else:
            royalty_puzhash = bytes32.from_hexstr(royalty_address)
        royalty_percentage = request.get("royalty_percentage", None)
        if royalty_percentage is None:
            royalty_percentage = uint16(0)
        else:
            royalty_percentage = uint16(int(royalty_percentage))
        metadata_list = []
        for meta in request["metadata_list"]:
            if "uris" not in meta.keys():
                return {"success": False, "error": "Data URIs is required"}
            if not isinstance(meta["uris"], list):
                return {"success": False, "error": "Data URIs must be a list"}
            if not isinstance(meta.get("meta_uris", []), list):
                return {"success": False, "error": "Metadata URIs must be a list"}
            if not isinstance(meta.get("license_uris", []), list):
                return {"success": False, "error": "License URIs must be a list"}
            nft_metadata = [
                ("u", meta["uris"]),
                ("h", hexstr_to_bytes(meta["hash"])),
                ("mu", meta.get("meta_uris", [])),
                ("lu", meta.get("license_uris", [])),
                ("sn", uint64(meta.get("edition_number", 1))),
                ("st", uint64(meta.get("edition_total", 1))),
            ]
            if "meta_hash" in meta and len(meta["meta_hash"]) > 0:
                nft_metadata.append(("mh", hexstr_to_bytes(meta["meta_hash"])))
            if "license_hash" in meta and len(meta["license_hash"]) > 0:
                nft_metadata.append(("lh", hexstr_to_bytes(meta["license_hash"])))
            metadata_program = Program.to(nft_metadata)
            metadata_dict = {
                "program": metadata_program,
                "royalty_pc": royalty_percentage,
                "royalty_ph": royalty_puzhash,
            }
            metadata_list.append(metadata_dict)
        target_address_list = request.get("target_list", None)
        target_list = []
        if target_address_list:
            for target in target_address_list:
                target_list.append(decode_puzzle_hash(target))
        mint_number_start = request.get("mint_number_start", 1)
        mint_total = request.get("mint_total", None)
        xch_coin_list = request.get("xch_coins", None)
        xch_coins = None
        if xch_coin_list:
            xch_coins = set([Coin.from_json_dict(xch_coin) for xch_coin in xch_coin_list])
        xch_change_target = request.get("xch_change_target", None)
        if xch_change_target is not None:
            if xch_change_target[:2] == "xch":
                xch_change_ph = decode_puzzle_hash(xch_change_target)
            else:
                xch_change_ph = bytes32(hexstr_to_bytes(xch_change_target))
        else:
            xch_change_ph = None
        new_innerpuzhash = request.get("new_innerpuzhash", None)
        new_p2_puzhash = request.get("new_p2_puzhash", None)
        did_coin_dict = request.get("did_coin", None)
        if did_coin_dict:
            did_coin = Coin.from_json_dict(did_coin_dict)
        else:
            did_coin = None
        did_lineage_parent_hex = request.get("did_lineage_parent", None)
        if did_lineage_parent_hex:
            did_lineage_parent = bytes32(hexstr_to_bytes(did_lineage_parent_hex))
        else:
            did_lineage_parent = None
        mint_from_did = request.get("mint_from_did", False)
        fee = uint64(request.get("fee", 0))
        if mint_from_did:
            sb = await nft_wallet.mint_from_did(
                metadata_list,
                mint_number_start=mint_number_start,
                mint_total=mint_total,
                target_list=target_list,
                xch_coins=xch_coins,
                xch_change_ph=xch_change_ph,
                new_innerpuzhash=new_innerpuzhash,
                new_p2_puzhash=new_p2_puzhash,
                did_coin=did_coin,
                did_lineage_parent=did_lineage_parent,
                fee=fee,
            )
        else:
            sb = await nft_wallet.mint_from_xch(
                metadata_list,
                mint_number_start=mint_number_start,
                mint_total=mint_total,
                target_list=target_list,
                xch_coins=xch_coins,
                xch_change_ph=xch_change_ph,
                fee=fee,
            )

        return {
            "success": True,
            "spend_bundle": sb,
        }

    async def get_farmed_amount(self, request) -> EndpointResult:
        tx_records: List[TransactionRecord] = await self.service.wallet_state_manager.tx_store.get_farming_rewards()
        amount = 0
        pool_reward_amount = 0
        farmer_reward_amount = 0
        fee_amount = 0
        last_height_farmed = 0
        for record in tx_records:
            if record.wallet_id not in self.service.wallet_state_manager.wallets:
                continue
            if record.type == TransactionType.COINBASE_REWARD:
                if self.service.wallet_state_manager.wallets[record.wallet_id].type() == WalletType.POOLING_WALLET:
                    # Don't add pool rewards for pool wallets.
                    continue
                pool_reward_amount += record.amount
            height = record.height_farmed(self.service.constants.GENESIS_CHALLENGE)
            # .get_farming_rewards() above queries for only confirmed records.  This
            # could be hinted by making TransactionRecord generic but streamable can't
            # handle that presently.  Existing code would have raised an exception
            # anyways if this were to fail and we already have an assert below.
            assert height is not None
            if record.type == TransactionType.FEE_REWARD:
                fee_amount += record.amount - calculate_base_farmer_reward(height)
                farmer_reward_amount += calculate_base_farmer_reward(height)
            if height > last_height_farmed:
                last_height_farmed = height
            amount += record.amount

        assert amount == pool_reward_amount + farmer_reward_amount + fee_amount
        return {
            "farmed_amount": amount,
            "pool_reward_amount": pool_reward_amount,
            "farmer_reward_amount": farmer_reward_amount,
            "fee_amount": fee_amount,
            "last_height_farmed": last_height_farmed,
        }

    async def create_signed_transaction(self, request, hold_lock=True) -> EndpointResult:
        if "wallet_id" in request:
            wallet_id = uint32(request["wallet_id"])
            wallet = self.service.wallet_state_manager.wallets[wallet_id]
        else:
            wallet = self.service.wallet_state_manager.main_wallet

        assert isinstance(
            wallet, (Wallet, CATWallet)
        ), "create_signed_transaction only works for standard and CAT wallets"

        if "additions" not in request or len(request["additions"]) < 1:
            raise ValueError("Specify additions list")

        additions: List[Dict] = request["additions"]
        amount_0: uint64 = uint64(additions[0]["amount"])
        assert amount_0 <= self.service.constants.MAX_COIN_AMOUNT
        puzzle_hash_0 = bytes32.from_hexstr(additions[0]["puzzle_hash"])
        if len(puzzle_hash_0) != 32:
            raise ValueError(f"Address must be 32 bytes. {puzzle_hash_0.hex()}")

        memos_0 = [] if "memos" not in additions[0] else [mem.encode("utf-8") for mem in additions[0]["memos"]]

        additional_outputs: List[AmountWithPuzzlehash] = []
        for addition in additions[1:]:
            receiver_ph = bytes32.from_hexstr(addition["puzzle_hash"])
            if len(receiver_ph) != 32:
                raise ValueError(f"Address must be 32 bytes. {receiver_ph.hex()}")
            amount = uint64(addition["amount"])
            if amount > self.service.constants.MAX_COIN_AMOUNT:
                raise ValueError(f"Coin amount cannot exceed {self.service.constants.MAX_COIN_AMOUNT}")
            memos = [] if "memos" not in addition else [mem.encode("utf-8") for mem in addition["memos"]]
            additional_outputs.append({"puzzlehash": receiver_ph, "amount": amount, "memos": memos})

        fee: uint64 = uint64(request.get("fee", 0))
        min_coin_amount: uint64 = uint64(request.get("min_coin_amount", 0))

        coins = None
        if "coins" in request and len(request["coins"]) > 0:
            coins = set([Coin.from_json_dict(coin_json) for coin_json in request["coins"]])

        exclude_coins = None
        if "exclude_coins" in request and len(request["exclude_coins"]) > 0:
            exclude_coins = set([Coin.from_json_dict(coin_json) for coin_json in request["exclude_coins"]])

        coin_announcements: Optional[Set[Announcement]] = None
        if (
            "coin_announcements" in request
            and request["coin_announcements"] is not None
            and len(request["coin_announcements"]) > 0
        ):
            coin_announcements = {
                Announcement(
                    bytes32.from_hexstr(announcement["coin_id"]),
                    hexstr_to_bytes(announcement["message"]),
                    hexstr_to_bytes(announcement["morph_bytes"])
                    if "morph_bytes" in announcement and len(announcement["morph_bytes"]) > 0
                    else None,
                )
                for announcement in request["coin_announcements"]
            }

        puzzle_announcements: Optional[Set[Announcement]] = None
        if (
            "puzzle_announcements" in request
            and request["puzzle_announcements"] is not None
            and len(request["puzzle_announcements"]) > 0
        ):
            puzzle_announcements = {
                Announcement(
                    bytes32.from_hexstr(announcement["puzzle_hash"]),
                    hexstr_to_bytes(announcement["message"]),
                    hexstr_to_bytes(announcement["morph_bytes"])
                    if "morph_bytes" in announcement and len(announcement["morph_bytes"]) > 0
                    else None,
                )
                for announcement in request["puzzle_announcements"]
            }

        async def _generate_signed_transaction() -> EndpointResult:
            if isinstance(wallet, Wallet):
                tx = await wallet.generate_signed_transaction(
                    amount_0,
                    bytes32(puzzle_hash_0),
                    fee,
                    coins=coins,
                    exclude_coins=exclude_coins,
                    ignore_max_send_amount=True,
                    primaries=additional_outputs,
                    memos=memos_0,
                    coin_announcements_to_consume=coin_announcements,
                    puzzle_announcements_to_consume=puzzle_announcements,
                    min_coin_amount=min_coin_amount,
                )
                signed_tx = tx.to_json_dict_convenience(self.service.config)

                return {"signed_txs": [signed_tx], "signed_tx": signed_tx}

            else:
                assert isinstance(wallet, CATWallet)

                txs = await wallet.generate_signed_transaction(
                    [amount_0] + [output["amount"] for output in additional_outputs],
                    [bytes32(puzzle_hash_0)] + [output["puzzlehash"] for output in additional_outputs],
                    fee,
                    coins=coins,
                    ignore_max_send_amount=True,
                    memos=[memos_0] + [output["memos"] for output in additional_outputs],
                    coin_announcements_to_consume=coin_announcements,
                    puzzle_announcements_to_consume=puzzle_announcements,
                    min_coin_amount=min_coin_amount,
                )
                signed_txs = [tx.to_json_dict_convenience(self.service.config) for tx in txs]

                return {"signed_txs": signed_txs, "signed_tx": signed_txs[0]}

        if hold_lock:
            async with self.service.wallet_state_manager.lock:
                return await _generate_signed_transaction()
        else:
            return await _generate_signed_transaction()

    ##########################################################################################
    # Pool Wallet
    ##########################################################################################
    async def pw_join_pool(self, request) -> EndpointResult:
        fee = uint64(request.get("fee", 0))
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(wallet, PoolWallet)
        if wallet.type() != uint8(WalletType.POOLING_WALLET):
            raise ValueError(f"Wallet with wallet id: {wallet_id} is not a plotNFT wallet.")

        pool_wallet_info: PoolWalletInfo = await wallet.get_current_state()
        owner_pubkey = pool_wallet_info.current.owner_pubkey
        target_puzzlehash = None

        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced.")

        if "target_puzzlehash" in request:
            target_puzzlehash = bytes32(hexstr_to_bytes(request["target_puzzlehash"]))
        assert target_puzzlehash is not None
        new_target_state: PoolState = create_pool_state(
            FARMING_TO_POOL,
            target_puzzlehash,
            owner_pubkey,
            request["pool_url"],
            uint32(request["relative_lock_height"]),
        )
        async with self.service.wallet_state_manager.lock:
            total_fee, tx, fee_tx = await wallet.join_pool(new_target_state, fee)
            return {"total_fee": total_fee, "transaction": tx, "fee_transaction": fee_tx}

    async def pw_self_pool(self, request) -> EndpointResult:
        # Leaving a pool requires two state transitions.
        # First we transition to PoolSingletonState.LEAVING_POOL
        # Then we transition to FARMING_TO_POOL or SELF_POOLING
        fee = uint64(request.get("fee", 0))
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        assert isinstance(wallet, PoolWallet)
        if wallet.type() != uint8(WalletType.POOLING_WALLET):
            raise ValueError(f"Wallet with wallet id: {wallet_id} is not a plotNFT wallet.")

        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced.")

        async with self.service.wallet_state_manager.lock:
            total_fee, tx, fee_tx = await wallet.self_pool(fee)
            return {"total_fee": total_fee, "transaction": tx, "fee_transaction": fee_tx}

    async def pw_absorb_rewards(self, request) -> EndpointResult:
        """Perform a sweep of the p2_singleton rewards controlled by the pool wallet singleton"""
        if await self.service.wallet_state_manager.synced() is False:
            raise ValueError("Wallet needs to be fully synced before collecting rewards")
        fee = uint64(request.get("fee", 0))
        max_spends_in_tx = request.get("max_spends_in_tx", None)
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        if wallet.type() != uint8(WalletType.POOLING_WALLET):
            raise ValueError(f"Wallet with wallet id: {wallet_id} is not a plotNFT wallet.")

        assert isinstance(wallet, PoolWallet)
        async with self.service.wallet_state_manager.lock:
            transaction, fee_tx = await wallet.claim_pool_rewards(fee, max_spends_in_tx)
            state: PoolWalletInfo = await wallet.get_current_state()
        return {"state": state.to_json_dict(), "transaction": transaction, "fee_transaction": fee_tx}

    async def pw_status(self, request) -> EndpointResult:
        """Return the complete state of the Pool wallet with id `request["wallet_id"]`"""
        wallet_id = uint32(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]

        if wallet.type() != WalletType.POOLING_WALLET.value:
            raise ValueError(f"Wallet with wallet id: {wallet_id} is not a plotNFT wallet.")

        assert isinstance(wallet, PoolWallet)
        state: PoolWalletInfo = await wallet.get_current_state()
        unconfirmed_transactions: List[TransactionRecord] = await wallet.get_unconfirmed_transactions()
        return {
            "state": state.to_json_dict(),
            "unconfirmed_transactions": unconfirmed_transactions,
        }

    ##########################################################################################
    # DataLayer Wallet
    ##########################################################################################
    async def create_new_dl(self, request) -> Dict:
        """Initialize the DataLayer Wallet (only one can exist)"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        dl_wallet: DataLayerWallet
        for _, wallet in self.service.wallet_state_manager.wallets.items():
            if WalletType(wallet.type()) == WalletType.DATA_LAYER:
                assert isinstance(wallet, DataLayerWallet)
                dl_wallet = wallet
                break
        else:
            async with self.service.wallet_state_manager.lock:
                dl_wallet = await DataLayerWallet.create_new_dl_wallet(
                    self.service.wallet_state_manager,
                    self.service.wallet_state_manager.main_wallet,
                )

        try:
            async with self.service.wallet_state_manager.lock:
                dl_tx, std_tx, launcher_id = await dl_wallet.generate_new_reporter(
                    bytes32.from_hexstr(request["root"]), fee=request.get("fee", uint64(0))
                )
                await self.service.wallet_state_manager.add_pending_transaction(dl_tx)
                await self.service.wallet_state_manager.add_pending_transaction(std_tx)
        except ValueError as e:
            log.error(f"Error while generating new reporter {e}")
            return {"success": False, "error": str(e)}

        return {
            "success": True,
            "transactions": [tx.to_json_dict_convenience(self.service.config) for tx in (dl_tx, std_tx)],
            "launcher_id": launcher_id,
        }

    async def dl_track_new(self, request) -> Dict:
        """Initialize the DataLayer Wallet (only one can exist)"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        peer: Optional[WSChiaConnection] = self.service.get_full_node_peer()
        if peer is None:
            raise ValueError("No peer connected")

        dl_wallet: DataLayerWallet
        for _, wallet in self.service.wallet_state_manager.wallets.items():
            if WalletType(wallet.type()) == WalletType.DATA_LAYER:
                assert isinstance(wallet, DataLayerWallet)
                dl_wallet = wallet
                break
        else:
            async with self.service.wallet_state_manager.lock:
                dl_wallet = await DataLayerWallet.create_new_dl_wallet(
                    self.service.wallet_state_manager,
                    self.service.wallet_state_manager.main_wallet,
                )
        await dl_wallet.track_new_launcher_id(bytes32.from_hexstr(request["launcher_id"]), peer)
        return {}

    async def dl_stop_tracking(self, request) -> Dict:
        """Initialize the DataLayer Wallet (only one can exist)"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        dl_wallet = self.service.wallet_state_manager.get_dl_wallet()
        if dl_wallet is None:
            raise ValueError("The DataLayer wallet has not been initialized")

        await dl_wallet.stop_tracking_singleton(bytes32.from_hexstr(request["launcher_id"]))
        return {}

    async def dl_latest_singleton(self, request) -> Dict:
        """Get the singleton record for the latest singleton of a launcher ID"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        for _, wallet in self.service.wallet_state_manager.wallets.items():
            if WalletType(wallet.type()) == WalletType.DATA_LAYER:
                assert isinstance(wallet, DataLayerWallet)
                only_confirmed = request.get("only_confirmed")
                if only_confirmed is None:
                    only_confirmed = False
                record = await wallet.get_latest_singleton(bytes32.from_hexstr(request["launcher_id"]), only_confirmed)
                return {"singleton": None if record is None else record.to_json_dict()}

        raise ValueError("No DataLayer wallet has been initialized")

    async def dl_singletons_by_root(self, request) -> Dict:
        """Get the singleton records that contain the specified root"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        for wallet in self.service.wallet_state_manager.wallets.values():
            if WalletType(wallet.type()) == WalletType.DATA_LAYER:
                assert isinstance(wallet, DataLayerWallet)
                records = await wallet.get_singletons_by_root(
                    bytes32.from_hexstr(request["launcher_id"]), bytes32.from_hexstr(request["root"])
                )
                records_json = [rec.to_json_dict() for rec in records]
                return {"singletons": records_json}

        raise ValueError("No DataLayer wallet has been initialized")

    async def dl_update_root(self, request) -> Dict:
        """Get the singleton record for the latest singleton of a launcher ID"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        for _, wallet in self.service.wallet_state_manager.wallets.items():
            if WalletType(wallet.type()) == WalletType.DATA_LAYER:
                assert isinstance(wallet, DataLayerWallet)
                async with self.service.wallet_state_manager.lock:
                    records = await wallet.create_update_state_spend(
                        bytes32.from_hexstr(request["launcher_id"]),
                        bytes32.from_hexstr(request["new_root"]),
                        fee=uint64(request.get("fee", 0)),
                    )
                    for record in records:
                        await self.service.wallet_state_manager.add_pending_transaction(record)
                    return {"tx_record": records[0].to_json_dict_convenience(self.service.config)}

        raise ValueError("No DataLayer wallet has been initialized")

    async def dl_update_multiple(self, request) -> Dict:
        """Update multiple singletons with new merkle roots"""
        if self.service.wallet_state_manager is None:
            return {"success": False, "error": "not_initialized"}

        for _, wallet in self.service.wallet_state_manager.wallets.items():
            if WalletType(wallet.type()) == WalletType.DATA_LAYER:
                assert isinstance(wallet, DataLayerWallet)
                async with self.service.wallet_state_manager.lock:
                    # TODO: This method should optionally link the singletons with announcements.
                    #       Otherwise spends are vulnerable to signature subtraction.
                    tx_records: List[TransactionRecord] = []
                    for launcher, root in request["updates"].items():
                        records = await wallet.create_update_state_spend(
                            bytes32.from_hexstr(launcher), bytes32.from_hexstr(root)
                        )
                        tx_records.extend(records)
                    # Now that we have all the txs, we need to aggregate them all into just one spend
                    modified_txs: List[TransactionRecord] = []
                    aggregate_spend = SpendBundle([], G2Element())
                    for tx in tx_records:
                        if tx.spend_bundle is not None:
                            aggregate_spend = SpendBundle.aggregate([aggregate_spend, tx.spend_bundle])
                            modified_txs.append(dataclasses.replace(tx, spend_bundle=None))
                    modified_txs[0] = dataclasses.replace(modified_txs[0], spend_bundle=aggregate_spend)
                    for tx in modified_txs:
                        await self.service.wallet_state_manager.add_pending_transaction(tx)
                    return {"tx_records": [rec.to_json_dict_convenience(self.service.config) for rec in modified_txs]}

        raise ValueError("No DataLayer wallet has been initialized")

    async def dl_history(self, request) -> Dict:
        """Get the singleton record for the latest singleton of a launcher ID"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        for _, wallet in self.service.wallet_state_manager.wallets.items():
            if WalletType(wallet.type()) == WalletType.DATA_LAYER:
                assert isinstance(wallet, DataLayerWallet)
                additional_kwargs = {}

                if "min_generation" in request:
                    additional_kwargs["min_generation"] = uint32(request["min_generation"])
                if "max_generation" in request:
                    additional_kwargs["max_generation"] = uint32(request["max_generation"])
                if "num_results" in request:
                    additional_kwargs["num_results"] = uint32(request["num_results"])

                history = await wallet.get_history(bytes32.from_hexstr(request["launcher_id"]), **additional_kwargs)
                history_json = [rec.to_json_dict() for rec in history]
                return {"history": history_json, "count": len(history_json)}

        raise ValueError("No DataLayer wallet has been initialized")

    async def dl_owned_singletons(self, request) -> Dict:
        """Get all owned singleton records"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        for _, wallet in self.service.wallet_state_manager.wallets.items():
            if WalletType(wallet.type()) == WalletType.DATA_LAYER:
                break
        else:
            raise ValueError("No DataLayer wallet has been initialized")

        assert isinstance(wallet, DataLayerWallet)
        singletons = await wallet.get_owned_singletons()
        singletons_json = [singleton.to_json_dict() for singleton in singletons]

        return {"singletons": singletons_json, "count": len(singletons_json)}

    async def dl_get_mirrors(self, request) -> Dict:
        """Get all of the mirrors for a specific singleton"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        for _, wallet in self.service.wallet_state_manager.wallets.items():
            if WalletType(wallet.type()) == WalletType.DATA_LAYER:
                break
        else:
            raise ValueError("No DataLayer wallet has been initialized")

        assert isinstance(wallet, DataLayerWallet)
        mirrors_json = []
        for mirror in await wallet.get_mirrors_for_launcher(bytes32.from_hexstr(request["launcher_id"])):
            mirrors_json.append(mirror.to_json_dict())

        return {"mirrors": mirrors_json}

    async def dl_new_mirror(self, request) -> Dict:
        """Add a new on chain message for a specific singleton"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        for _, wallet in self.service.wallet_state_manager.wallets.items():
            if WalletType(wallet.type()) == WalletType.DATA_LAYER:
                dl_wallet = wallet
                break
        else:
            raise ValueError("No DataLayer wallet has been initialized")

        assert isinstance(dl_wallet, DataLayerWallet)
        async with self.service.wallet_state_manager.lock:
            txs = await dl_wallet.create_new_mirror(
                bytes32.from_hexstr(request["launcher_id"]),
                request["amount"],
                [bytes(url, "utf8") for url in request["urls"]],
                fee=request.get("fee", uint64(0)),
            )
            for tx in txs:
                await self.service.wallet_state_manager.add_pending_transaction(tx)

        return {
            "transactions": [tx.to_json_dict_convenience(self.service.config) for tx in txs],
        }

    async def dl_delete_mirror(self, request) -> Dict:
        """Remove an existing mirror for a specific singleton"""
        if self.service.wallet_state_manager is None:
            raise ValueError("The wallet service is not currently initialized")

        peer: Optional[WSChiaConnection] = self.service.get_full_node_peer()
        if peer is None:
            raise ValueError("No peer connected")

        for _, wallet in self.service.wallet_state_manager.wallets.items():
            if WalletType(wallet.type()) == WalletType.DATA_LAYER:
                assert isinstance(wallet, DataLayerWallet)
                dl_wallet: DataLayerWallet = wallet
                break
        else:
            raise ValueError("No DataLayer wallet has been initialized")

        async with self.service.wallet_state_manager.lock:
            txs = await dl_wallet.delete_mirror(
                bytes32.from_hexstr(request["coin_id"]),
                peer,
                fee=request.get("fee", uint64(0)),
            )
            for tx in txs:
                await self.service.wallet_state_manager.add_pending_transaction(tx)

        return {
            "transactions": [tx.to_json_dict_convenience(self.service.config) for tx in txs],
        }
