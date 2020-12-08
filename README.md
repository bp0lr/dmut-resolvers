# dmut-resolvers

this repo generate resolvers.txt using github actions.
The task is runned one time a day.

The process include:
- download a public list from https://public-dns.info/nameservers.txt
- run this list again [https://github.com/bp0lr/dnsfaster](dnsfaster)
- update this repo whit the result.


This file is generated to be used by [https://github.com/bp0lr/dmut](dmut)
