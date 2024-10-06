import random
card = {"card_number": 0, "pin": 0}
iin = "400000"
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
        print(card["card_number"])
        break
    else:
        card['card_number'] = iin + str(random.randrange(0000000000, 9999999999))
        if len(card["card_number"]) < 16:
            card['card_number'] = iin + str(random.randrange(0000000000, 9999999999))
            print('bad')