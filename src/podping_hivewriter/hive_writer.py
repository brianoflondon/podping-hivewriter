import asyncio
import json
import logging
from sys import getsizeof
from timeit import default_timer as timer
from typing import Set, Tuple

import beem
import zmq
import zmq.asyncio
from beem.account import Account
from beem.exceptions import AccountDoesNotExistsException, MissingKeyError
from beemapi.exceptions import UnhandledRPCError

from podping_hivewriter.config import Config
from podping_hivewriter.get_hive_config import get_podping_settings


def get_hive():
    posting_key = Config.posting_key
    if Config.test:
        node = Config.TEST_NODE[0]
        hive = beem.Hive(keys=posting_key, node=node)
        logging.info("---------------> Using Test Node " + node)
    else:
        hive = beem.Hive(keys=posting_key)
        logging.info("---------------> Using Main Hive Chain ")
    return hive


async def hive_startup(ignore_errors=False, resource_test=True) -> beem.Hive:
    """Run though a startup sequence connect to Hive and check env variables
    Exit with error unless ignore_errors passed as True
    Defaults to sending two startup resource_test posts and checking resources"""
    error_messages = []
    # Set up Hive with error checking
    logging.info(
        "Podping startup sequence initiated, please stand by, "
        "full bozo checks in operation..."
    )
    if not Config.server_account:
        error_messages.append(
            "No Hive account passed: HIVE_SERVER_ACCOUNT environment var must be set."
        )
        logging.error(error_messages[-1])

    if not Config.posting_key:
        error_messages.append(
            "No Hive Posting Key passed: HIVE_POSTING_KEY environment var must be set."
        )
        logging.error(error_messages[-1])

    try:
        hive = get_hive()
        await get_podping_settings(Config.CONTROL_ACCOUNT)

    except Exception as ex:
        error_messages.append(f"{ex} occurred {ex.__class__}")
        error_messages.append(f"Can not connect to Hive, probably bad key")
        logging.error(error_messages[-1])
        error_messages.append("I'm sorry, Dave, I'm afraid I can't do that")
        logging.error(error_messages[-1])
        exit_message = " - ".join(error_messages)
        raise SystemExit(exit_message)

    acc = None
    try:
        acc = Account(Config.server_account, blockchain_instance=hive, lazy=True)
        allowed = get_allowed_accounts()
        if Config.server_account not in allowed:
            error_messages.append(
                f"Account @{Config.server_account} not authorised to send Podpings"
            )
            logging.error(error_messages[-1])
    except AccountDoesNotExistsException:
        error_messages.append(
            f"Hive account @{Config.server_account} does not exist, "
            f"check ENV vars and try again AccountDoesNotExistsException"
        )
        logging.error(error_messages[-1])
    except Exception as ex:
        error_messages.append(f"{ex} occurred {ex.__class__}")
        logging.error(error_messages[-1])

    if resource_test:
        if acc:
            try:  # Now post two custom json to test.
                manabar = acc.get_rc_manabar()
                logging.info(
                    f"Testing Account Resource Credits"
                    f' - before {manabar.get("current_pct"):.2f}%'
                )
                custom_json = {
                    "server_account": Config.server_account,
                    "USE_TEST_NODE": Config.test,
                    "message": "Podping startup initiated",
                }
                error_message, success = send_notification(
                    custom_json, hive, "podping-startup"
                )

                if not success:
                    error_messages.append(error_message)
                logging.info("Testing Account Resource Credits.... 5s")
                await asyncio.sleep(2)
                manabar_after = acc.get_rc_manabar()
                logging.info(
                    f"Testing Account Resource Credits"
                    f' - after {manabar_after.get("current_pct"):.2f}%'
                )
                cost = manabar.get("current_mana") - manabar_after.get("current_mana")
                if cost == 0:  # skip this test if we're going to get ZeroDivision
                    capacity = 1000000
                else:
                    capacity = manabar_after.get("current_mana") / cost
                logging.info(f"Capacity for further podpings : {capacity:.1f}")
                custom_json["v"] = Config.CURRENT_PODPING_VERSION
                custom_json["capacity"] = f"{capacity:.1f}"
                custom_json["message"] = "Podping startup complete"
                error_message, success = send_notification(
                    custom_json, hive, "podping-startup"
                )
                if not success:
                    error_messages.append(error_message)

            except Exception as ex:
                error_messages.append(f"{ex} occurred {ex.__class__}")
                logging.error(error_messages[-1])

    if error_messages:
        error_messages.append("I'm sorry, Dave, I'm afraid I can't do that")
        logging.error(
            "Startup of Podping status: I'm sorry, Dave, I'm afraid I can't do that."
        )
        exit_message = " - ".join(error_messages)
        if not Config.test or ignore_errors:
            raise SystemExit(exit_message)

    logging.info("Startup of Podping status: SUCCESS! Hit the BOOST Button.")
    logging.info(
        f"---------------> {Config.server_account} <- Hive Account will be used"
    )

    return hive


def get_allowed_accounts(acc_name="podping") -> Set[str]:
    """get a list of all accounts allowed to post by acc_name (podping)
    and only react to these accounts"""
    # Ignores test node.
    h = beem.Hive(node="https://api.hive.blog")
    master_account = Account(acc_name, blockchain_instance=h, lazy=True)
    return set(master_account.get_following())


def send_notification(
    data, hive: beem.Hive, operation_id="podping"
) -> Tuple[str, bool]:
    """Sends a custom_json to Hive
    Expects two env variables, Hive account name and posting key
    HIVE_SERVER_ACCOUNT
    HIVE_POSTING_KEY
    """
    num_urls = 0

    if type(data) == set:
        num_urls = len(data)
        size_of_urls = len("".join(data).encode("UTF-8"))
        custom_json = {
            "v": Config.CURRENT_PODPING_VERSION,
            "num_urls": num_urls,
            "r": Config.NOTIFICATION_REASONS["feed_update"],
            "urls": list(data),
        }
    elif type(data) == str:
        num_urls = 1
        size_of_urls = len(data.encode("UTF-8"))
        custom_json = {
            "v": Config.CURRENT_PODPING_VERSION,
            "num_urls": 1,
            "r": Config.NOTIFICATION_REASONS["feed_update"],
            "url": data,
        }
    elif type(data) == dict:
        size_of_urls = getsizeof(data)
        custom_json = data
    else:
        logging.error(f"Unknown data type: {data}")
        return f"Unknown data type: {data}", False

    try:
        # Artificially create errors <-----------------------------------
        # if operation_id == "podping" and Config.errors:
        #     r = randint(1, 100)
        #     if r <= Config.errors:
        #         raise Exception(
        #             f"Infinite Improbability Error level of {r}% : "
        #             f"Threshold set at {Config.errors}%"
        #         )

        # Assert Exception:o.json.length() <= HIVE_CUSTOM_OP_DATA_MAX_LENGTH:
        # Operation JSON must be less than 8192 bytes.
        size_of_json = len(json.dumps(custom_json).encode("UTF-8"))
        tx = hive.custom_json(
            id=operation_id,
            json_data=custom_json,
            required_posting_auths=[Config.server_account],
        )
        trx_id = tx["trx_id"]
        logging.info(
            f"Transaction sent: {trx_id} - Num urls: {num_urls}"
            f" - Size of Urls: {size_of_urls} - Json size: {size_of_json}"
        )
        logging.info(f"Overhead: {size_of_json - size_of_urls}")
        return trx_id, True

    except MissingKeyError:
        error_message = f"The provided key for @{Config.server_account} is not valid "
        logging.error(error_message)
        return error_message, False
    except UnhandledRPCError as ex:
        error_message = f"{ex} occurred: {ex.__class__}"
        logging.error(error_message)
        trx_id = error_message
        return trx_id, False

    except Exception as ex:
        error_message = f"{ex} occurred {ex.__class__}"
        logging.error(error_message)
        trx_id = error_message
        return trx_id, False


async def send_notification_worker(
    hive_queue: "asyncio.Queue[Set[str]]", hive: beem.Hive
):
    """Opens and watches a queue and sends notifications to Hive one by one"""
    while True:
        try:
            url_set = await hive_queue.get()
        except RuntimeError:
            return
        start = timer()
        trx_id, failure_count = await failure_retry(url_set, hive)
        duration = timer() - start
        hive_queue.task_done()
        logging.info(f"Task time: {duration:0.2f} - Queue size: {hive_queue.qsize()}")
        logging.info(f"Finished a task: {trx_id} - {failure_count}")


async def url_q_worker(
    url_queue: "asyncio.Queue[str]", hive_queue: "asyncio.Queue[Set[str]]"
):
    async def get_from_queue():
        try:
            return await url_queue.get()
        except RuntimeError:
            return

    while True:
        url_set: Set[str] = set()
        start = timer()
        duration = 0
        urls_size_without_commas = 0
        urls_size_total = 0

        # Wait until we have enough URLs to fit in the payload
        # or get into the current Hive block
        while (
            duration < Config.HIVE_OPERATION_PERIOD
            and urls_size_total < Config.MAX_URL_LIST_BYTES
        ):
            #  get next URL from Q
            logging.debug(f"Duration: {duration:.3f} - WAITING - Queue: {len(url_set)}")
            try:
                url = await asyncio.wait_for(
                    get_from_queue(), timeout=Config.HIVE_OPERATION_PERIOD
                )
                url_set.add(url)
                url_queue.task_done()

                logging.info(
                    f"Duration: {duration:.3f} - URL in queue: {url}"
                    f" - URL List: {len(url_set)}"
                )

                # byte size of URL in JSON is URL + 2 quotes
                urls_size_without_commas += len(url.encode("UTF-8")) + 2

                # Size of payload in bytes is
                # length of URLs in bytes + the number of commas + 2 square brackets
                # Assuming it's a JSON list eg ["https://...","https://"..."]
                urls_size_total = urls_size_without_commas + len(url_set) - 1 + 2
            except asyncio.TimeoutError:
                pass
            except RuntimeError:
                return
            except Exception as ex:
                logging.error(f"{ex} occurred")
            finally:
                # Always get the time of the loop
                duration = timer() - start

        try:
            if len(url_set):
                await hive_queue.put(url_set)
                logging.info(f"Size of Urls: {urls_size_total}")
        except RuntimeError:
            return
        except Exception as ex:
            logging.error(f"{ex} occurred")


async def failure_retry(
    url_set: Set[str], hive: beem.Hive, failure_count=0
) -> Tuple[str, int]:
    if failure_count >= len(Config.HALT_TIME):
        # Give up.
        error_message = (
            f"I'm sorry Dave, I'm afraid I can't do that. "
            f"Too many tries {failure_count}"
        )
        logging.error(error_message)
        raise SystemExit(error_message)

    if failure_count > 0:
        logging.error(f"Waiting {Config.HALT_TIME[failure_count]}s")
        await asyncio.sleep(Config.HALT_TIME[failure_count])
        logging.info(f"RETRYING num_urls: {len(url_set)}")
    else:
        if type(url_set) == set:
            logging.info(f"Received num_urls: {len(url_set)}")
        elif type(url_set) == str:
            logging.info(f"One URL Received: {url_set}")
        else:
            logging.info(f"{url_set}")

    trx_id, success = send_notification(url_set, hive)
    if success:
        return trx_id, failure_count
    else:
        return await failure_retry(url_set, hive, failure_count + 1)


async def zmq_response_loop(url_queue: "asyncio.Queue[str]", loop=None):
    if not loop:
        loop = asyncio.get_event_loop()

    context = zmq.asyncio.Context()
    socket = context.socket(zmq.REP, io_loop=loop)
    if Config.bind_all:
        socket.bind(f"tcp://*:{Config.zmq}")
    else:
        socket.bind(f"tcp://127.0.0.1:{Config.zmq}")

    while True:
        url: str = await socket.recv_string()
        await url_queue.put(url)
        ans = "OK"
        await socket.send_string(ans)

    socket.close()


async def url_only_startup(url: str):
    hive = await hive_startup(resource_test=False)

    await failure_retry(url, hive)


def task_startup(hive: beem.Hive, loop=None):
    if not loop:  # pragma: no cover
        loop = asyncio.get_event_loop()

    # Adding a Queue system to the Hive send_notification section
    hive_queue: "asyncio.Queue[Set[str]]" = asyncio.Queue(loop=loop)
    # Move the URL Q into a proper Q
    url_queue: "asyncio.Queue[str]" = asyncio.Queue(loop=loop)

    loop.create_task(send_notification_worker(hive_queue, hive))
    loop.create_task(url_q_worker(url_queue, hive_queue))
    loop.create_task(zmq_response_loop(url_queue, loop))
    loop.create_task(update_podping_settings_worker(Config.CONTROL_ACCOUNT))


def loop_running_startup_task(hive_task: asyncio.Task):
    hive = hive_task.result()
    task_startup(hive)


async def update_podping_settings(acc_name) -> None:
    """Take newly found settings and put them into Config"""
    podping_settings = await get_podping_settings("podping")
    if podping_settings:
        logging.info("Configuration overide from Podping Hive")
        Config.NOTIFICATION_REASONS = podping_settings.get("NOTIFICATION_REASONS")
        Config.HIVE_OPERATION_PERIOD = podping_settings.get("HIVE_OPERATION_PERIOD")
        Config.MAX_URL_LIST_BYTES = podping_settings.get("MAX_URL_LIST_BYTES")
    return


async def update_podping_settings_worker(acc_name) -> None:
    """Worker to check for changed settings every (period)"""
    while True:
        await update_podping_settings(acc_name)
        await asyncio.sleep(Config.CONTROL_ACCOUNT_CHECK_PERIOD)


async def get_podping_settings(acc_name) -> dict:
    """Returns podping settings if they exist"""
    hive = beem.Hive()
    acc = Account(acc_name, blockchain_instance=hive, lazy=True)
    posting_meta = json.loads(acc["posting_json_metadata"])
    podping_settings = posting_meta.get("podping-settings")
    if podping_settings:
        return podping_settings
    else:
        return None


def run(loop=None):
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s %(levelname)s %(name)s %(threadName)s : %(message)s",
    )

    if not loop:  # pragma: no cover
        loop = asyncio.new_event_loop()

    Config.setup()

    if Config.url:
        if loop.is_running():
            loop.create_task(url_only_startup(Config.url))
        else:
            asyncio.run(url_only_startup(Config.url))
        return

    if not loop.is_running():  # pragma: no cover
        try:
            hive = asyncio.run(hive_startup(resource_test=True))
            task_startup(hive, loop)
            loop.run_forever()
        except KeyboardInterrupt:
            pass
        finally:
            loop.close()
    else:
        hive_task = loop.create_task(hive_startup(resource_test=True))
        hive_task.add_done_callback(loop_running_startup_task)


if __name__ == "__main__":
    run()
