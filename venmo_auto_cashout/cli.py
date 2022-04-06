import argparse
from os import getenv
from time import sleep
from typing import List

from sentry_sdk import start_span, start_transaction
from venmo_api import Client, Transaction
from venmo_auto_cashout.lunchmoney import generate_rules


def run_cli():
    parser = argparse.ArgumentParser(
        description="Automatically cash-out your Venmo balance as individual transfers"
    )

    parser.add_argument(
        "--token",
        type=str,
        default=getenv("VENMO_API_TOKEN"),
        required=not getenv("VENMO_API_TOKEN"),
        help="Your venmo API token",
    )
    parser.add_argument(
        "--lunchmoney-email",
        type=str,
        default=getenv("LUNCHMONEY_EMAIL"),
        help="Authenticate with Lunchmoney to add matching rules on cashout",
    )
    parser.add_argument(
        "--lunchmoney-password",
        type=str,
        default=getenv("LUNCHMONEY_PASSWORD"),
    )
    parser.add_argument(
        "--lunchmoney-otp-secret",
        type=str,
        default=getenv("LUNCHMONEY_OTP_SECRET"),
    )
    parser.add_argument(
        "--quiet", action=argparse.BooleanOptionalAction, help="Do not produce any output"
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        help="Do not actually initiate bank transfers",
    )

    args = parser.parse_args()

    def output(msg: str):
        if not args.quiet:
            print(msg)

    with start_transaction(op="cashout", name="cashout") as tran:
        tran.set_tag("dry_run", args.dry_run)

        # Venmo API client
        with start_span(op="init_venmo_client"):
            venmo = Client(access_token=args.token)

        me = venmo.my_profile()
        if not me:
            raise Exception("Failed to load Venmo profile")

        current_balance: int = me.balance
        tran.set_tag("cashout_balance", "${:,.2f}".format(current_balance / 100))

        if current_balance == 0:
            tran.set_tag("has_transactions", False)
            output("Your venmo balance is zero. Nothing to do")
            return

        # Sleep for 5 seconds to make sure the transactions actually show up
        output("Your balance is ${:,.2f}".format(current_balance / 100))
        output("Waiting 5 seconds before querying transactions...")
        sleep(5.0)

        # XXX: There may be some leftover amount if the transactions do not match
        # up exactly to the current account balance.
        remaining_balance = current_balance
        eligable_transactions: List[Transaction] = []

        with start_span(op="get_transactions"):
            transactions = venmo.user.get_user_transactions(user=me)
            if transactions is None:
                raise Exception("Failed to load trnasctions")

            # Produce a list of eligible transactions
            for transaction in transactions:
                # Ignore transactions that were not paying us
                if transaction.payee.username != me.username:
                    continue

                # There are no more eligable transactions once we have accounted for
                # all of our account balance
                if transaction.amount > remaining_balance:
                    break

                remaining_balance = remaining_balance - transaction.amount
                eligable_transactions.append(transaction)

        tran.set_tag("has_transactions", len(eligable_transactions) > 0)
        tran.set_tag("transaction_count", len(eligable_transactions))

        tran.set_data(
            "transactions",
            [
                {"payer": t.payer.display_name, "amount": t.amount, "note": t.note}
                for t in eligable_transactions
            ],
        )

        # Show some details about what we're about to do
        output("There are {} transactions to cash-out".format(len(eligable_transactions)))

        if len(eligable_transactions) > 0:
            output("")

        for transaction in eligable_transactions:
            output(
                " -> Transfer: ${price:,.2f} -- {name} ({note})".format(
                    name=transaction.payer.display_name,
                    price=transaction.amount / 100,
                    note=transaction.note,
                )
            )

        if remaining_balance > 0:
            output(" -> Transfer: ${:,.2f} of remaining balance".format(remaining_balance / 100))

        # Nothing left to do in dry-run mode
        if args.dry_run:
            output("\ndry-run -- Not initiating transfers")
            return

        # Do the transactions
        with start_span(op="initiate_transfer"):
            for transaction in eligable_transactions:
                venmo.transfer.initiate_transfer(amount=transaction.amount)

            if remaining_balance > 0:
                venmo.transfer.initiate_transfer(amount=remaining_balance)

        # Create lunchmoney rules for each transaction
        if args.lunchmoney_email is not None:
            with start_span(op="lunchmoney_create_rules"):
                generate_rules(
                    transactions=eligable_transactions,
                    email=args.lunchmoney_email,
                    password=args.lunchmoney_password,
                    otp_secret=args.lunchmoney_otp_secret,
                )

        output("\nAll money transfered out!")
