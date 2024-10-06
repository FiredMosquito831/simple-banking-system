import random

card = {"card_number": 0, "pin": 0}
iin = "400000"
balance = 0

while True:
    print("""
1. Create an account 
2. Log into account 
0. Exit
""")
    value = int(input())
    if value == 1:

        card['card_number'] = iin + str(random.randrange(0000000000, 9999999999))
        card['pin'] = random.randrange(1000, 9999)
        print(f"""
Your card has been created
Your card number:
{card['card_number']}
Your card PIN:
{card['pin']}\n
""")

    elif value == 2:
        card_num = int(input())
        pinn = int(input())
        if card_num == int(card['card_number']) and pinn == int(card['pin']):
            print("You have successfully logged in!")
            print("""
1. Balance
2. Log out
0. Exit\n
""")
            while True:
                value1 = int(input())
                if value1 == 1:
                    print(balance)
                    print("""
1. Balance
2. Log out
0. Exit\n
""")
                elif value1 == 2:
                    print("You have successfully logged out!")
                    break
                else:
                    print("Bye!")
                    exit()
        else:
            print("Wrong card number or PIN!")

    else:
        print("Bye!")
        break