import random
choice = 4
value = 4
card = {"card_number": 0, "pin": 0}
iin = "400000"
balance = 0
check = 0
while value != 0:
    print("""
1. Create an account 
2. Log into account 
0. Exit
\n""")
    value = int(input())
    if value == 1:
        card['card_number'] = iin + str(random.randrange(0000000000, 9999999999))
        while True:
            if len(card["card_number"]) < 16:
                card['card_number'] = iin + str(random.randrange(0000000000, 9999999999))
            else:
                break
        while True:
            cardNo = card["card_number"]
            nDigits = len(cardNo)
            nSum = 0
            isSecond = False
            for i in range(nDigits - 1, -1, -1):
                d = ord(cardNo[i]) - ord('0')
                if (isSecond == True):
                    d = d * 2
                nSum += d // 10
                nSum += d % 10
                isSecond = not isSecond
            if (nSum % 10 == 0):
                break
            else:
                card['card_number'] = iin + str(random.randrange(0000000000, 9999999999))
                if len(card["card_number"]) < 16:
                    card['card_number'] = iin + str(random.randrange(0000000000, 9999999999))

        card['pin'] = random.randrange(1000, 9999)
        print(f"""Your card has been created
Your card number: 
{card["card_number"]}
Your card PIN:
{card["pin"]}\n""")
    elif value == 2:
        print("Enter you card number: ")
        number = input()
        print("Enter your PIN")
        PIN = int(input())
        if number == card["card_number"] and PIN == card["pin"]:
            print("You have successfully logged in!")
            while choice != 0:
                print("""1. Balance
2. Log out
0. Exit\n""")
                choice = int(input())
                if choice == 1:
                    print(balance)
                elif choice == 2:
                    print("You have successfully logged out!")
                    break
                elif choice == 0:
                    print("Bye!")
                    exit()
        elif number != card["card_number"] or int(PIN) != card["pin"]:
            print("Wrong card number or PIN")
    elif value == 0:
        print("Bye!")
        exit()