# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Dataset shard manipulation.

For information about FlatBuffers see https://flatbuffers.dev/
"""

from pathlib import Path
import sys

import flatbuffers
import numpy as np

from sedpack.io.compress import CompressedFile
from sedpack.io.metadata import Attribute, DatasetStructure
from sedpack.io.types import ExampleT, CompressionT
from sedpack.io.shard.shard_writer_base import ShardWriterBase

# Autogenerated from src/sedpack/io/flatbuffer/shard.fbs
import sedpack.io.flatbuffer.shardfile.Attribute as fbapi_Attribute
import sedpack.io.flatbuffer.shardfile.Example as fbapi_Example
import sedpack.io.flatbuffer.shardfile.Shard as fbapi_Shard


class ShardWriterFlatBuffer(ShardWriterBase):
    """Shard writing capabilities.
    """

    def __init__(self, dataset_structure: DatasetStructure,
                 shard_file: Path) -> None:
        """Collect information about a new shard.

        Args:

            dataset_structure (DatasetStructure): The structure of data being
            saved.

            shard_file (Path): Full path to the shard file.
        """
        assert dataset_structure.shard_file_type == "fb"

        super().__init__(
            dataset_structure=dataset_structure,
            shard_file=shard_file,
        )

        self._examples: list = []

        self._builder: flatbuffers.Builder | None = None

    def _write(self, values: ExampleT) -> None:
        """Write an example on disk. Writing may be buffered.

        Args:

            values (ExampleT): Attribute values.
        """
        if self._builder is None:
            self._builder = flatbuffers.Builder(0)

        # Since we are not saving attribute names we need to make sure to
        # iterate in the correct order.
        saved_attributes: list = []
        for attribute in self.dataset_structure.saved_data_description:
            attribute_bytes = self.save_numpy_vector_as_bytearray(
                builder=self._builder,
                attribute=attribute,
                value=values[attribute.name],
            )

            fbapi_Attribute.AttributeStart(self._builder)
            fbapi_Attribute.AttributeAddAttributeBytes(self._builder,
                                                       attribute_bytes)
            saved_attributes.append(fbapi_Attribute.AttributeEnd(
                self._builder))

        # Save attributes vector.
        fbapi_Example.ExampleStartAttributesVector(self._builder,
                                                   len(saved_attributes))
        for offset in reversed(saved_attributes):
            self._builder.PrependUOffsetTRelative(offset)
        attributes_vector_offset = self._builder.EndVector()

        # Save the example.
        fbapi_Example.ExampleStart(self._builder)
        fbapi_Example.ExampleAddAttributes(self._builder,
                                           attributes_vector_offset)
        self._examples.append(fbapi_Example.ExampleEnd(self._builder))

    @staticmethod
    def save_numpy_vector_as_bytearray(builder: flatbuffers.Builder,
                                       attribute: Attribute,
                                       value: np.ndarray) -> int:
        """Save a given array into a FlatBuffer as bytes. This is to ensure
        compatibility with types which are not supported by FlatBuffers (e.g.,
        np.float16).  The FlatBuffers schema must mark this vector as type
        bytes [byte] (see src/sedpack/io/flatbuffer/shard.fbs) since there is a
        distinction of how the length is being saved. The inverse of this
        function is
        `sedpack.io.flatbuffer.iterate.IterateShardFlatBuffer.decode_array`.

        If we have an array of np.int32 of 10 elements the FlatBuffers library
        would save the length as 10. Which is then impossible to read in Rust
        since the length and itemsize (sizeof of the type) are private. Thus we
        could not get the full array back. Thus we are saving the array as 40
        bytes. This function does not modify the `value`, and saves a flattened
        version of it. This function also saves the exact dtype as given by
        `attribute`. Bytes are being saved in little endian ("<") and
        c_contiguous ("C") order, same as with FlatBuffers. Alignment is set to
        `dtype.itemsize` as opposed to FlatBuffers choice of `dtype.alignment`.

        Args:

          builder (flatbuffers.Builder): The byte buffer being constructed.
          Must be initialized.

          attribute (Attribute): Description of this attribute (shape and dtype).

          value (np.ndarray): The array to be saved. The shape should be as
          defined in `attribute` (will be flattened).

        Returns: The offset returned by `flatbuffers.Builder.EndVector`.
        """
        # Not sure about flatbuffers.Builder __bool__ semantics.
        assert builder is not None

        # See `flatbuffers.builder.Builder.CreateNumpyVector`.

        # Copy the value in order not to modify the original and flatten for
        # better saving.
        value = np.copy(value).flatten()

        # This is the workaround when the user passes a value which is
        # wrong dtype. Then a different number of bytes could be saved than
        # read causing unpredictable issues.
        if not np.can_cast(value, to=attribute.dtype, casting="safe"):
            raise ValueError(f"Cannot cast value of dtype {value.dtype} "
                             f"passed as {attribute = }")
        value = np.array(value, dtype=attribute.dtype)

        # Ensure little endian which is needed for FlatBuffers.
        match value.dtype.byteorder:
            case "=":
                # Native, check sys
                match sys.byteorder:
                    case "big":
                        value = value.byteswap(inplace=False)
                    case "little":
                        pass
                    case _:
                        raise ValueError("Unexpected sys.byteorder")
            case ">":
                # Big endian we need to byteswap.
                value = value.byteswap(inplace=False)
            case "<" | "|":
                # Either little endian (good for us) or not applicable
                # (distinction does not matter).
                pass
            case _:
                # Should not happen according to NumPy.
                raise ValueError("Unexpected value of byteorder for attribute"
                                 f" {attribute.name}")

        # This is going to be saved, ensure c_contiguous ordering.
        byte_representation = value.tobytes(order="C")

        # Total length of the array (in bytes).
        length: int = len(byte_representation)

        # Start a vector and move head accordingly.
        builder.StartVector(
            elemSize=1,  # Storing bytes.
            numElems=length,
            alignment=value.dtype.itemsize,  # Cautious alignment of the array.
        )
        builder.head = int(builder.Head() - length)

        # Copy values.
        builder.Bytes[builder.Head():builder.Head() +
                      length] = byte_representation

        # Not sure why the length is being set again (potentially allowing
        # recursive structures?).
        builder.vectorNumElems = length

        # Return the vector offset.
        return builder.EndVector()

    def close(self) -> None:
        """Close the shard file(-s).
        """
        if not self._examples:
            # Nothing to save.
            assert not self._shard_file.is_file()
            return

        # Save examples vector.
        fbapi_Shard.ShardStartExamplesVector(self._builder,
                                             len(self._examples))
        for offset in reversed(self._examples):
            self._builder.PrependUOffsetTRelative(offset)
        examples_vector_offset = self._builder.EndVector()

        # Save the shard.
        fbapi_Shard.ShardStart(self._builder)
        fbapi_Shard.ShardAddExamples(self._builder, examples_vector_offset)
        shard = fbapi_Shard.ShardEnd(self._builder)

        # Finish the builder.
        self._builder.Finish(shard)

        # Write the buffer into a file.
        with CompressedFile(self.dataset_structure.compression).open(
                self._shard_file, "wb") as file:
            file.write(self._builder.Output())

        self._builder = None
        assert self._shard_file.is_file()

    @staticmethod
    def supported_compressions() -> list[CompressionT]:
        """Return a list of supported compression types.
        """
        return CompressedFile.supported_compressions()
