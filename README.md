[![Generic badge](https://img.shields.io/badge/Status-Pre_Alpha-red.svg)](#)
[![Generic badge](https://img.shields.io/badge/Python-3.7+-blue.svg)](#)
[![Generic badge](https://img.shields.io/badge/OS-Linux%20|%20macOS%20|%20Windows-blue.svg)](#)

**This is experimental code. The file format may change.**

# [dmk: dark matter keeper](https://github.com/rtmigo/dmk_py#readme)

`dmk` allows you to store files, passwords or other private data in an encrypted 
file.

Each encrypted entry is independent. The **secret name** of entry decrypts only
that entry. It reveals nothing about other entries, even whether they exist.

No master password. No table of contents. The number of entries cannot be determined. 

The file consists of unidentifiable data. Secret name is necessary to discover
the data of particular entry. The rest of the data remain dark matter.

# Install

``` 
$ pip3 install dmk
```

# Secret names

The secret name serves as both:

- the name of the entry
- the password

It is a secret. And it must be unique.

For example, information about a credit card credentials can be stored under name
`"crEd1tcard"` or `"visa_secret123"`.

Longer secret names mean better encryption.

# Save and read text

When called without parameters, the `get` and `set` commands query for all 
values interactively:

``` 
$ dmk set

Secret name: secRet007
Repeat secret name: secRet007 
Text: My darling's jokes are not so funny
```

``` 
$ dmk get

Secret name: secRet007
 
My darling's jokes are not so funny
```

Interactive input is optional. You can get by with one line:

``` 
$ dmk set -e secRet007 -t "My darling's jokes are not so funny"
```

``` 
$ dmk get -e secRet007

My darling's jokes are not so funny
```



# Save and read file

Read data from a `source.doc` and save it as encrypted entry `secRet007`

```  
$ dmk set -e secRet007 /my/docs/source.doc
```

Decrypt the entry `secRet007` and write the result to `target.doc`

``` bash
$ dmk get -e secRet007 /my/docs/target.doc
```

The `-e` parameter is optional. If it is not specified, the value will be
prompted for interactive input.

Add dummy data
==============

Part of the vault file contains dummy data. This data cannot be decrypted.
Dummy data only increases the size of the storage, thus hiding the amount 
of real data.

Each time the file is updated, a random amount of dummy data is added and removed. 
The change can be up to 5% of the file size.

You can also add dummy data manually, to make sure the file is big enough.

Make the vault file 2 megabytes larger:

```
dmk dummy 2M
```


Make the vault file 500 kilobytes larger:

```
dmk dummy 500K
```


Keep in mind:

- Dummy data added in this way cannot be removed
- Vault speed linearly depends on its size. If you increase the vault 10 times, 
  then the search for data in it will go 10 times slower

Vault location
==============

Entries will be stored in a file.

You can check the current vault file location with `vault` command:

```
$ dmk vault
```
Output:
```
/home/username/vault.dmk
```

By default, it is `vault.dmk` in the current user's `$HOME` directory.

--------------------------------------------------------------------------------

The `-v` parameter overrides the location for a single run.

```
$ dmk -v /path/to/myfile.data vault
```

Output:
```
/path/to/myfile.data
```

The parameter can be used with any commands:

```
$ dmk -v /path/to/myfile.data set 
$ dmk -v /path/to/myfile.data get 
```

--------------------------------------------------------------------------------

The `$DMK_VAULT_FILE` environment variable overrides the default location:

``` 
$ export DMK_VAULT_FILE=/path/to/myfile.data
$ dmk vault  
```
Output:
```
/path/to/myfile.data
```

While `$DMK_VAULT_FILE` is set all the command will use `myfile.data`:

```
$ dmk set   # set to myfile.data 
$ dmk get   # get from myfile.data
```

# Under the hood

- Entries are encrypted 
- Number of entries cannot be determined
- File format is unidentifiable

## Size obfuscation

The vault file stores all data within multiple fixed-size blocks.

Small entries are padded so they become block-sized. Large entries are split and
padded to fit into multiple blocks. In the end, they are all just a lot of
blocks.

A block gives absolutely no information for someone who does not own the
secret name. All non-random data is either hashed or encrypted. The size of padding
is unknown.

The number of blocks is no secret. Their contents are secret.

- The number of blocks is random. Many blocks are dummy. They are
  indistinguishable from real data, but do not contain anything meaningful

- The information about which entry the block belongs to is cryptographically
  protected. It is impossible to even figure out if two blocks belong to the same
  entry

- Random actions are taken every time the vault is updated: some dummy blocks are
  added, and some are removed

Thus, **number and size of entries cannot be determined** by the size of the
vault file or number of blocks.

Only the following is known:
- The payload is smaller than the file size
- The number of entries is less than the number of blocks

By the way, the file may contain zero entries.

## File obfuscation

The vault file format is **indistinguishable from random data**.

The file has no signatures, no header, no constant bytes (or even bits), no
block boundaries. File size will not give clues: the file is randomly padded
with a size that is not a multiple of a block.

The only predictable part of the file is a version identifier encoded in
the first two bytes. But the similar "version number" can be found literally 
in every fourth file in the world. Those two bytes are not even constant.

## Block encryption

1) **URandom** creates 38-bytes **salt** when we initialize the vault file. The
   salt is saved openly in the file. This salt never changes. It is required for
   any other actions on the vault.

2) **Argon2id** (memory 128 MiB, iterations 4, parallelism 8) derives 
   256-bit **private key** from salted (1) secret name.

3) **ChaCha20** encrypts the block data using the 256-bit private key (2) and 
   newly generated 96-bit urandom **block nonce**.

4) The encrypted data of the block starts with a 40-byte header. This header
   contains the secret key in plain text, and some other information.
   The header is followed by the **header checksum**, which is a 160-bit 
   **Blake2s** hash. The checksum itself is also in the encrypted stream.
   
   When decrypting, we are directly or indirectly checking that all components 
   match each other:
   - 32-byte (256-bit) private key (1) - it decrypts the data
   - 20-byte (160-bit) header checksum (4)
   - decrypted secret name (4) up to 28 bytes long
   - the secret name provided by user
   
   If everything matches everything, it is at least a 53 bytes match. We also 
   verified that this is not a private key collision or a checksum collision. 
   Still not deterministic, but more likely than any conceivable coincidence. 
   This is indeed a block related to the given secret name.
   
   We also made sure that the data decryption is proceeding correctly.

5) **CRC-32** checksum verifies the entry data decrypted from the block.

   This verification occurs when we have already checked (4) the correctness
   of the private key (2). Therefore, it is really only a self-test to see
   if the data is decoded as expected.

   This checksum is saved inside the encrypted stream. If the data in the 
   blocks is the same, it will not be noticeable from the outside due to 
   different nonce (3) values.





